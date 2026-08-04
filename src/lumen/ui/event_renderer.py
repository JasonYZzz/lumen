"""Run-event rendering loop, extracted verbatim from ``app.py``.

``EventRendererMixin`` holds the public :meth:`render_event` seam, the big
``_render_event`` isinstance dispatcher, and the assistant-stream / compaction
row helpers it drives. ``LumenApp`` is only imported under ``TYPE_CHECKING``
to avoid a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.widgets import Static

from lumen.approval import ApprovalMode
from lumen.events import (
    ClarificationRequested,
    CommentaryDelta,
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    InputDelivered,
    InputDequeued,
    InputQueued,
    PlanCreated,
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
    ToolApprovalBatchPending,
    ToolApprovalPending,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from lumen.resources import CONTROL_TOOL_NAMES
from lumen.ui.activity_indicator import RunActivityIndicator
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.composer import PromptEditor
from lumen.ui.plan_panel import PlanPanel
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.streaming_markdown import AssistantMarkdown, StreamingMarkdownController
from lumen.ui.tool_card import ToolCard

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
        status = self.query_one("#status", Static)
        activity = self.query_one(RunActivityIndicator)
        if not isinstance(event, CommentaryDelta):
            self._close_commentary_segment()
        if isinstance(event, InputQueued | InputDelivered | InputDequeued):
            self._refresh_interactive_queue()
        elif isinstance(event, RunStarted):
            await self._close_assistant_segment()
            self._active_plan_panel = None
            self.query_one(PlanReviewPanel).hide()
            status.update(self._status("Thinking…"))
            activity.start("Thinking")
        elif isinstance(event, TextDelta):
            await self._ensure_assistant_segment(messages)
            assert self._assistant_stream is not None
            self._assistant_stream.append(event.text)
            if activity.label != "Writing response":
                activity.describe("Writing response")
        elif isinstance(event, TextRetracted):
            document = self._assistant_container
            await self._close_assistant_segment()
            if document is not None:
                await document.remove()
        elif isinstance(event, CommentaryDelta):
            await self._close_assistant_segment()
            await self._append_commentary(event.text)
            activity.describe("Thinking", "reviewing intermediate results")
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
            active_step = next(
                (step.title for step in event.plan.steps if step.status.value == "in_progress"),
                None,
            )
            activity.describe("Updating tasks", active_step, tone="mode-plan")
        elif isinstance(event, ProgressReported):
            await self._close_assistant_segment()
            await self._append_progress(event.summary, event.next_action)
            activity.describe("Working", event.next_action or event.summary)
        elif isinstance(event, ToolCallStarted):
            if event.origin == "control" or event.name in CONTROL_TOOL_NAMES:
                await self._finish_timeline_update(messages, follow)
                return
            # A tool call terminates the current contiguous assistant-text
            # segment. Any later TextDelta gets a new Markdown widget after
            # this card, preserving the actual event order.
            await self._close_assistant_segment()
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
            card.start(args=event.args, origin=event.origin, risk=event.risk, started_at=event.started_at)
            status.update(self._status(f"Running {event.name}…"))
            activity.describe_tool(event.name, event.args)
        elif isinstance(event, ToolCallFinished):
            if event.name in CONTROL_TOOL_NAMES:
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
                )
            activity.describe("Reviewing result", event.name.replace("_", " "), tone="tool")
        elif isinstance(event, ToolApprovalPending):
            card = self._tool_cards.get(event.call_id)
            if card is None:
                card = ToolCard(event.call_id, event.name)
                self._tool_cards[event.call_id] = card
                await messages.mount(card)
            card.mark_approval_pending(event)
            self.query_one(ApprovalPanel).enqueue(event)
            status.update(self._status(f"Approval required: {event.name}"))
            activity.describe("Waiting for approval", event.name.replace("_", " "))
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
            activity.describe("Waiting for batch approval", event.risk_summary)
        elif isinstance(event, ToolApprovalResolved):
            approval_panel = self.query_one(ApprovalPanel)
            approval_panel.resolve(event.call_id)
            card = self._tool_cards.get(event.call_id)
            if card is not None:
                card.resolve_approval(approved=event.approved)
            status.update(self._status("Ready" if event.approved else "Denied"))
            if approval_panel.active_request is None:
                self.query_one("#prompt", PromptEditor).focus()
            activity.describe("Continuing", "tool approved" if event.approved else "tool denied")
        elif isinstance(event, ContextCompactionStarted):
            await self._close_assistant_segment()
            await self._update_compaction_row("Compacting context…")
            activity.describe("Compacting context")
        elif isinstance(event, ContextCompactionCompleted):
            await self._update_compaction_row(
                f"Context compacted: {event.active_message_count} active messages."
            )
        elif isinstance(event, ContextCompactionFailed):
            await self._update_compaction_row(f"Context compaction failed: {event.message}")
        elif isinstance(event, UsageUpdated):
            status.update(self._status_line(event))
        elif isinstance(event, RunCompleted):
            # Force a final flush so the rendered markdown reflects every
            # token of the streamed answer before we mark the run done.
            await self._close_assistant_segment()
            self._last_assistant_output = event.output
            status.update(self._status("Ready"))
            activity.stop()
            if self._approval_mode is ApprovalMode.PLAN and event.output.strip():
                self.query_one(PlanReviewPanel).show(step_count=len(self.plan.steps))
        elif isinstance(event, ClarificationRequested):
            await self._close_assistant_segment()
            choices = "\n".join(f"- {choice}" for choice in event.choices)
            await self._append_system(event.question + (f"\n{choices}" if choices else ""))
            status.update(self._status("Waiting for your answer"))
            activity.describe("Waiting for input", event.question)
        elif isinstance(event, RunWaitingForUser):
            await self._close_assistant_segment()
            status.update(self._status("Waiting for your answer"))
            activity.stop()
            self.query_one("#prompt", PromptEditor).focus()
        elif isinstance(event, RunFailed):
            await self._close_assistant_segment()
            await self._append_system(f"Run failed: {event.message}")
            status.update(self._status("Run failed"))
            activity.stop()
        elif isinstance(event, RunCancelled):
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

    async def _update_compaction_row(self: LumenApp, text: str) -> None:
        """Render compaction state into one mutable row, not three messages."""

        messages = self.query_one("#messages", VerticalScroll)
        if self._compaction_row is None:
            self._compaction_row = Static("", classes="compaction-row", markup=False)
            await messages.mount(self._compaction_row)
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
