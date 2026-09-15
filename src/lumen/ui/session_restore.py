"""Session-state application and timeline restoration, extracted from ``app.py``.

``SessionRestoreMixin`` copies authoritative coordinator state into transient
render state and rebuilds the message timeline from the timeline store when a
session is created, resumed, or cleared. ``LumenApp`` is only imported under
``TYPE_CHECKING`` to avoid a circular import.
"""

# Cooperative Textual mixin; see approval_controller.py for the intersection-
# self limitation behind these local suppressions.
# pyright: reportGeneralTypeIssues=false, reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import VerticalScroll
from textual.lazy import Lazy
from textual.widgets import Static

from lumen.plan import PlanState
from lumen.run_coordinator import CoordinatorState
from lumen.timeline import (
    RepositoryTimelineAdapter,
    TimelineItem,
    TimelineKind,
    TimelineStore,
)
from lumen.ui.plan_panel import PlanPanel
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.streaming_markdown import AssistantMarkdown
from lumen.ui.tool_card import ToolCard
from lumen.ui.transcript_blocks import CommentaryBlock
from lumen.ui.welcome import WelcomePanel

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class SessionRestoreMixin:
    """Apply coordinator state and (re)build timeline widgets for a session."""

    async def _clear_visible_timeline(self: LumenApp) -> None:
        """Clear rendered activity without changing the model conversation.

        This mirrors mature coding-agent TUIs: ``/clear`` is a view operation,
        while ``/new`` is the explicit context/session boundary.
        """

        messages = self.query_one("#messages", VerticalScroll)
        for child in list(messages.children):
            await child.remove()
        self._tool_cards.clear()
        self._compaction_row = None
        self._active_plan_panel = None
        self._assistant_container = None
        self._assistant_active = None
        self._assistant_frozen_count = 0
        await messages.mount(WelcomePanel())
        self._refresh_welcome_panel()
        self._follow_tail = True
        self.query_one("#new-activity", Static).remove_class("visible")
        self._scroll_timeline_end(messages)

    async def _apply_coordinator_state(
        self: LumenApp, state: CoordinatorState, *, restored: bool = False
    ) -> None:
        """Copy authoritative coordinator state into transient render state."""

        previous_session_id = self.session.id if self.session is not None else None
        self.session = state.session
        self.history = list(state.history)
        self.plan = state.plan
        session_data = self.resources.session_repository.load(state.session.id)
        self._transcript_density = session_data.settings.transcript_density
        self.last_prompt = state.last_user_input
        # Restore THIS session's compaction summary so iterative compaction
        # continues from it — never a stale App-level value carried over from
        # a previous session. Session state switches are atomic: all four
        # fields are set together before any render happens.
        self._last_compaction_summary = state.compaction_summary
        if previous_session_id != state.session.id:
            self._last_assistant_output = ""
            try:
                self.query_one(PlanReviewPanel).hide()
            except Exception:
                pass
            self.timeline_store = TimelineStore(
                RepositoryTimelineAdapter(self.resources.session_repository, state.session.id)
            )
            if previous_session_id is not None:
                messages = self.query_one("#messages", VerticalScroll)
                for child in list(messages.children):
                    await child.remove()
                self._tool_cards.clear()
                self._compaction_row = None
                self._active_plan_panel = None
                if not restored:
                    await messages.mount(WelcomePanel())
        if restored:
            self.timeline_store.load_older(limit=20)
            await self._restore_timeline_widgets()
            self._after_session_load()

    def _after_session_load(self: LumenApp) -> None:
        panels = list(self.query(PlanPanel))
        self._active_plan_panel = panels[-1] if panels else None
        restored_text = self._restored_notice()
        if restored_text:
            messages = self.query_one("#messages", VerticalScroll)
            messages.mount(Lazy(Static(restored_text, classes="restored-notice", markup=False)))

    async def _restore_timeline_widgets(self: LumenApp) -> None:
        messages = self.query_one("#messages", VerticalScroll)
        for child in list(messages.children):
            await child.remove()
        items = self.timeline_store.window()
        latest_answer = next(
            (item.text for item in reversed(items) if item.kind is TimelineKind.ASSISTANT),
            "",
        )
        self._last_assistant_output = latest_answer
        for item in items:
            widget = self._timeline_widget(item)
            if widget is not None:
                await messages.mount(widget)
        await self._prune_timeline_widgets(messages)
        self._scroll_timeline_end(messages)

    def _timeline_widget(self: LumenApp, item: TimelineItem) -> Any:
        if item.kind is TimelineKind.USER:
            return Lazy(Static(f"» {item.text}", classes="user-message", markup=False))
        if item.kind is TimelineKind.ASSISTANT:
            return AssistantMarkdown(item.text, classes="assistant-message")
        if item.kind is TimelineKind.PLAN and item.plan is not None:
            return PlanPanel(PlanState.model_validate(item.plan))
        if item.kind is TimelineKind.COMMENTARY:
            block = CommentaryBlock(expanded=self._transcript_density == "verbose")
            block.append(item.text)
            return Lazy(block)
        if item.kind is TimelineKind.PROGRESS:
            return Lazy(AssistantMarkdown(f"↳ {item.text}", classes="progress-block"))
        if item.kind is TimelineKind.TOOL and item.call_id and item.tool_name:
            card = ToolCard(item.call_id, item.tool_name)
            card.start(args=item.args or {}, origin="session", risk="recorded")
            card.set_density(self._transcript_density)
            if item.result is not None:
                card.update_result(
                    result=item.result,
                    preview=item.preview,
                    is_error=item.is_error,
                )
            else:
                card.compact()
            self._tool_cards[item.call_id] = card
            return card
        css = "system-message" if item.kind is not TimelineKind.ERROR else "system-message is-error"
        return Lazy(Static(item.text, classes=css, markup=False))

    def _restored_notice(self: LumenApp) -> str:
        steps = len(self.plan.steps)
        completed = sum(1 for s in self.plan.steps if s.status.value == "completed")
        return (
            f"Resumed session: plan has {completed}/{steps} steps completed, "
            f"{len(self.history)} messages in active context."
        )
