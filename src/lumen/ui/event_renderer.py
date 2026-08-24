"""Run-event rendering loop, extracted verbatim from ``app.py``.

``EventRendererMixin`` holds the public :meth:`render_event` seam, the big
``_render_event`` isinstance dispatcher, and the assistant-stream / compaction
row helpers it drives. ``LumenApp`` is only imported under ``TYPE_CHECKING``
to avoid a circular import.
"""

# Cooperative Textual mixin; see approval_controller.py for the intersection-
# self limitation behind these local suppressions.
# pyright: reportGeneralTypeIssues=false, reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.widgets import Static

from lumen.collaboration import CollaborationMode
from lumen.events import (
    AgentLifecycleChanged,
    ClarificationRequested,
    CommentaryDelta,
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    InputDelivered,
    InputDequeued,
    InputQueued,
    PlanCreated,
    PlanReviewPending,
    PlanReviewResolved,
    PlanUpdated,
    ProgressReported,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    RunWaitingForUser,
    TextDelta,
    TextRetracted,
    ThinkingDelta,
    ToolApprovalBatchPending,
    ToolApprovalPending,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
    WorkProductChanged,
)
from lumen.task_control import CONTROL_TOOL_NAMES
from lumen.ui.activity_indicator import (
    RunActivityIndicator,
    tool_activity_presentation,
)
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.composer import PromptEditor
from lumen.ui.plan_panel import PlanPanel
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.streaming_markdown import AssistantMarkdown, StreamingMarkdownController
from lumen.ui.tool_card import ToolCard
from lumen.ui.transcript_blocks import ReadToolGroup

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class EventRendererMixin:
    """Render the 28 structured run events into the message timeline."""

    async def render_event(self: LumenApp, event: RunEvent) -> None:
        """Public UI-adapter seam for rendering one structured run event."""

        await self._render_event(event)

    async def _render_event(self: LumenApp, event: RunEvent) -> None:
        messages = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(messages)
        self.timeline_store.apply(event)
        # Welcome is a zero-state, not part of the conversation. Any event
        # that represents real run activity removes it before timeline layout
        # is measured. Usage and queue bookkeeping may occur while the app is
        # still idle, so those events deliberately leave it in place.
        if not isinstance(event, UsageUpdated | InputQueued | InputDelivered | InputDequeued):
            await self._dismiss_welcome()
        status = self.query_one("#status", Static)
        activity = self.query_one(RunActivityIndicator)
        if not isinstance(event, CommentaryDelta):
            self._close_commentary_segment()
        if not isinstance(event, ThinkingDelta):
            self._close_thinking_segment()
        if not isinstance(event, ToolCallStarted | ToolCallFinished):
            self._current_read_group = None
        if isinstance(event, InputQueued | InputDelivered | InputDequeued):
            self._refresh_interactive_queue()
        elif isinstance(event, RunStarted):
            await self._close_assistant_segment()
            self.query_one("#prompt", PromptEditor).placeholder = "Ask Lumen…"
            self._active_plan_panel = None
            self.query_one(PlanReviewPanel).hide()
            status.update(self._status("Thinking…"))
            activity.start("Thinking")
            if self.config.ui.terminal_title:
                self.title = f"Lumen · working · {self.resources.workspace.name}"
        elif isinstance(event, TextDelta):
            await self._ensure_assistant_segment(messages)
            assert self._assistant_stream is not None
            self._assistant_stream.append(event.text)
            activity.suspend("Writing response")
        elif isinstance(event, TextRetracted):
            document = self._assistant_container
            await self._close_assistant_segment()
            if document is not None:
                await document.remove()
        elif isinstance(event, CommentaryDelta):
            await self._close_assistant_segment()
            await self._append_commentary(event.text)
            activity.stop()
        elif isinstance(event, ThinkingDelta):
            await self._close_assistant_segment()
            await self._append_thinking(event.text)
            activity.suspend("Thinking")
        elif isinstance(event, PlanCreated | PlanUpdated):
            self.plan = event.plan
            await self._close_assistant_segment()
            panel = self._active_plan_panel
            if isinstance(event, PlanCreated) or panel is None or panel.parent is None:
                panel = PlanPanel(event.plan)
                self._active_plan_panel = panel
                await messages.mount(panel)
            else:
                panel.update_plan(event.plan)
            activity.stop()
        elif isinstance(event, ProgressReported):
            await self._close_assistant_segment()
            await self._append_progress(event.summary, event.next_action)
            activity.stop()
        elif isinstance(event, WorkProductChanged):
            await self._close_assistant_segment()
            resource = f" — {event.resource}" if event.resource else ""
            detail = event.summary or event.status
            await self._append_system(f"Work product {event.phase}{resource}: {detail}")
            activity.stop()
        elif isinstance(event, AgentLifecycleChanged):
            await self._close_assistant_segment()
            detail = event.summary or event.status
            await self._append_system(f"Agent {event.path} · {event.phase}: {detail}")
            # Child progress keeps streaming while the parent run is active;
            # killing the activity indicator per step would make it flicker.
            if event.phase != "progress":
                activity.stop()
        elif isinstance(event, PlanReviewPending):
            self.plan = event.plan
            self._plan_review_revision = event.revision
            self.query_one(PlanReviewPanel).show(
                step_count=len(event.plan.steps),
                current_mode=self._approval_mode.value,
                revision=event.revision,
            )
            status.update(self._status("Plan ready for review"))
            activity.stop()
            if self.config.ui.terminal_title:
                self.title = f"Lumen · review plan · {self.resources.workspace.name}"
            if self.config.ui.notifications:
                self.notify(f"Plan revision {event.revision} is ready for review", timeout=4)
        elif isinstance(event, PlanReviewResolved):
            self.query_one(PlanReviewPanel).hide()
            if event.approved:
                self._collaboration_mode = CollaborationMode.DEFAULT
            status.update(self._status("Executing approved plan" if event.approved else "Replanning"))
        elif isinstance(event, ToolCallStarted):
            if event.origin == "control" or event.name in CONTROL_TOOL_NAMES:
                await self._finish_timeline_update(messages, follow)
                return
            # A tool call terminates the current contiguous assistant-text
            # segment. Any later TextDelta gets a new Markdown widget after
            # this card, preserving the actual event order.
            await self._close_assistant_segment()
            presentation = tool_activity_presentation(
                event.name,
                event.args,
                origin=event.origin,
                risk=event.risk,
                view=event.call_view,
            )
            use_activity_group = presentation.groupable and self._transcript_density == "normal"
            if use_activity_group:
                group = self._current_read_group
                if group is None or group.parent is None or not group.is_compatible(presentation):
                    group = ReadToolGroup()
                    self._current_read_group = group
                    await messages.mount(group)
                group.start_call(event.call_id, event.name, presentation)
                self._read_tool_groups[event.call_id] = group
                status.update(self._status(f"Running {event.name}…"))
                activity.suspend(
                    presentation.active_verb,
                    presentation.detail,
                    tone=presentation.tone,
                )
                await self._finish_timeline_update(messages, follow)
                return
            self._current_read_group = None
            card = self._tool_cards.get(event.call_id)
            if card is None:
                card = ToolCard(event.call_id, event.name)
                self._tool_cards[event.call_id] = card
                await messages.mount(card)
                # Prune old entries from the tracking dict to prevent
                # unbounded growth in long sessions. The widgets stay in the
                # timeline (Lazy-rendered); we only stop tracking them for
                # live updates. Pending approval cards are never pruned.
                if len(self._tool_cards) > self._TOOL_CARD_MAX:
                    for old_id in list(self._tool_cards.keys()):
                        if old_id in self._approval_waiters:
                            continue  # never prune pending approvals
                        if old_id != event.call_id:
                            del self._tool_cards[old_id]
                        if len(self._tool_cards) <= self._TOOL_CARD_MAX:
                            break
            card.start(
                args=event.args,
                origin=event.origin,
                risk=event.risk,
                started_at=event.started_at,
                call_view=event.call_view,
            )
            card.set_density(self._transcript_density)
            status.update(self._status(f"Running {event.name}…"))
            activity.suspend(
                presentation.active_verb,
                presentation.detail,
                tone=presentation.tone,
            )
        elif isinstance(event, ToolCallFinished):
            if event.name in CONTROL_TOOL_NAMES:
                await self._finish_timeline_update(messages, follow)
                return
            group = self._read_tool_groups.pop(event.call_id, None)
            if group is not None:
                group.finish_call(event.call_id, is_error=event.is_error)
                activity.start("Thinking")
                await self._finish_timeline_update(messages, follow)
                return
            card = self._tool_cards.get(event.call_id)
            if card is not None:
                card.update_result(
                    result=event.result,
                    preview=event.preview,
                    is_error=event.is_error,
                    elapsed_seconds=event.elapsed_seconds,
                    exit_code=event.exit_code,
                    result_view=event.result_view,
                )
            activity.start("Thinking")
        elif isinstance(event, ToolApprovalPending):
            card = self._tool_cards.get(event.call_id)
            if card is None:
                card = ToolCard(event.call_id, event.name)
                self._tool_cards[event.call_id] = card
                await messages.mount(card)
            card.mark_approval_pending(event)
            card.set_density(self._transcript_density)
            self.query_one(ApprovalPanel).enqueue(event)
            status.update(self._status(f"Approval required: {event.name}"))
            activity.stop()
            if self.config.ui.terminal_title:
                self.title = f"Lumen · approval required · {self.resources.workspace.name}"
            if self.config.ui.notifications:
                self.notify(f"Approval required: {event.name}", severity="warning", timeout=5)
        elif isinstance(event, ToolApprovalBatchPending):
            for request in event.requests:
                pending = ToolApprovalPending(
                    request.call_id,
                    request.name,
                    request.args,
                    request.origin,
                    request.risk,
                )
                card = self._tool_cards.get(request.call_id)
                if card is None:
                    card = ToolCard(request.call_id, request.name)
                    self._tool_cards[request.call_id] = card
                    await messages.mount(card)
                card.mark_approval_pending(pending)
            self.query_one(ApprovalPanel).enqueue_batch(event)
            status.update(self._status(f"Approval required: {event.risk_summary}"))
            activity.stop()
        elif isinstance(event, ToolApprovalResolved):
            approval_panel = self.query_one(ApprovalPanel)
            approval_panel.resolve(event.call_id)
            card = self._tool_cards.get(event.call_id)
            if card is not None:
                card.resolve_approval(approved=event.approved)
            status.update(self._status("Ready" if event.approved else "Denied"))
            if approval_panel.active_request is None:
                self.query_one("#prompt", PromptEditor).focus()
            activity.start("Thinking")
        elif isinstance(event, ContextCompactionStarted):
            await self._close_assistant_segment()
            await self._update_compaction_row("Compacting context…")
            activity.describe("Compacting context")
        elif isinstance(event, ContextCompactionCompleted):
            await self._update_compaction_row(
                f"Context compacted: {event.active_message_count} active messages."
            )
        elif isinstance(event, ContextCompactionFailed):
            await self._update_compaction_row(f"✗ Context compaction failed: {event.message}", failed=True)
        elif isinstance(event, UsageUpdated):
            status.update(self._status_line(event))
        elif isinstance(event, RunCompleted):
            # Force a final flush so the rendered markdown reflects every
            # token of the streamed answer before we mark the run done.
            await self._close_assistant_segment()
            self._last_assistant_output = event.output
            status.update(self._status("Ready"))
            activity.stop()
            if self.config.ui.terminal_title:
                self.title = f"Lumen · ready · {self.resources.workspace.name}"
        elif isinstance(event, ClarificationRequested):
            await self._close_assistant_segment()
            choices = "\n".join(f"- {choice}" for choice in event.choices)
            await self._append_system(event.question + (f"\n{choices}" if choices else ""))
            status.update(self._status("Waiting for your answer"))
            activity.describe("Waiting for input", event.question)
            if self.config.ui.terminal_title:
                self.title = f"Lumen · input required · {self.resources.workspace.name}"
            if self.config.ui.notifications:
                self.notify("Agent needs your input", severity="warning", timeout=5)
        elif isinstance(event, RunWaitingForUser):
            await self._close_assistant_segment()
            status.update(self._status("Waiting for your answer"))
            activity.stop()
            self.query_one("#prompt", PromptEditor).focus()
        elif isinstance(event, RunFailed):
            await self._close_assistant_segment()
            await self._append_error(f"Run failed: {event.message} · /retry to resend")
            status.update(self._status("Run failed"))
            activity.stop()
            if self.config.ui.terminal_title:
                self.title = f"Lumen · failed · {self.resources.workspace.name}"
            # 失败不再额外发 toast:错误行 + 状态栏 + 终端标题已是三重信号,
            # 而顶部右侧的 toast 会恰好盖住时间线里的错误行本体(含 /retry 提示)。
        elif isinstance(event, RunCancelled):  # pyright: ignore[reportUnnecessaryIsInstance]
            # The isinstance guard stays even though the current RunEvent union
            # makes it provably narrowed: a future event type must fall through
            # silently here, not be misrendered as a cancellation.
            await self._close_assistant_segment()
            await self._append_system("Run cancelled.")
            status.update(self._status("Cancelled"))
            activity.stop()
        await self._finish_timeline_update(messages, follow)

    async def _ensure_assistant_segment(self: LumenApp, messages: VerticalScroll) -> None:
        """Mount one complete Markdown document for an assistant segment."""

        if self._assistant_stream is not None:
            return
        document = AssistantMarkdown("", classes="assistant-message")
        self._assistant_container = document
        await messages.mount(document)

        async def render_markdown(text: str) -> None:
            render_follow = self._capture_timeline_follow(messages)
            document.set_source(text)
            await self._finish_timeline_update(messages, render_follow)

        self._assistant_stream = StreamingMarkdownController(render_markdown)

    async def _close_assistant_segment(self: LumenApp) -> None:
        """Flush and close the active contiguous assistant-text segment.

        The container and its rendered blocks remain in the timeline as the
        answer; we only drop the live-streaming references so the next
        :class:`TextDelta` starts a fresh segment.
        """

        stream = self._assistant_stream
        try:
            if stream is not None:
                await stream.close()
        except Exception as error:
            # Rendering is a view concern. Preserve the runtime's original
            # completed/failed/cancelled event even if Rich rejects a frame.
            self.notify(f"Markdown render failed: {error}", severity="error")
        finally:
            self._assistant_stream = None
            self._assistant_container = None

    async def _flush_assistant_now(self: LumenApp) -> None:
        """Force every buffered token through the serialized renderer."""

        if self._assistant_stream is not None:
            await self._assistant_stream.flush()

    async def _update_compaction_row(self: LumenApp, text: str, *, failed: bool = False) -> None:
        """Render compaction state into one mutable row, not three messages.

        ``failed`` switches the row to error color so a failed compaction
        reads as a failure, not as routine progress metadata.
        """

        messages = self.query_one("#messages", VerticalScroll)
        if self._compaction_row is None:
            self._compaction_row = Static("", classes="compaction-row", markup=False)
            await messages.mount(self._compaction_row)
        self._compaction_row.set_class(failed, "is-error")
        self._compaction_row.update(text)

    def _clear_compaction_row(self: LumenApp) -> None:
        """Remove the compaction row so a new/resumed session starts clean.

        The row is session-scoped state: it reflects the previous session's
        last compaction and must not linger into a new one. We drop the widget
        reference so the next compaction re-mounts a fresh row.
        """
        if self._compaction_row is not None:
            try:
                self._compaction_row.remove()
            except Exception:
                pass
            self._compaction_row = None
