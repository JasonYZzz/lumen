"""Timeline scroll-follow (tail-tracking) logic, extracted verbatim from ``app.py``.

The suggested functional split proved impractical: every helper reads and
writes several pieces of ``LumenApp`` state (``_follow_tail``,
``_last_tail_scroll_y``, ``_tail_follow_generation``, ``_approval_waiters``,
``_tool_cards`` …), so a pure-function API would mean threading that state
through every call. A mixin keeps the code byte-identical while moving it out
of the God Object. ``LumenApp`` is only imported under ``TYPE_CHECKING`` to
avoid a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.widgets import Static

from lumen.ui.tool_card import ToolCard

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class ScrollFollowMixin:
    """Tail-follow, pruning, and older-page loading for the message timeline."""

    def _capture_timeline_follow(self: LumenApp, container: VerticalScroll) -> bool:
        """Snapshot tail state before content changes its scroll extent."""

        if self._assistant_stream is not None and self._follow_tail:
            # A render frame may have increased Markdown height before the
            # deferred scroll-to-end callback runs. That transient lag is not
            # evidence that the user intentionally left the tail.
            return True
        target_y = float(container.scroll_target_y)
        # Hiding the activity row or resizing the viewport may reduce
        # ``max_scroll_y`` and clamp the target upward.  Compare against the
        # last reachable tail, not the previous (now impossible) coordinate;
        # otherwise a layout shrink is mistaken for an explicit user scroll.
        reachable_last_tail = min(self._last_tail_scroll_y, float(container.max_scroll_y))
        if self._follow_tail and target_y < reachable_last_tail:
            # A user scroll command updates the target before the animated /
            # deferred position necessarily moves. Treat that as an explicit
            # departure from the tail so a queued refresh cannot pull them
            # back down.
            self._follow_tail = False
        elif container.max_scroll_y <= 0 or container.is_vertical_scroll_end:
            self._follow_tail = True
            self._last_tail_scroll_y = float(container.scroll_y)
        elif self._follow_tail and float(container.scroll_y) >= self._last_tail_scroll_y:
            # Layout can grow after a previous scroll_end (Markdown rendering,
            # Lazy widgets, welcome panel). The viewport then temporarily no
            # longer reports "at end" even though the user never scrolled.
            # Preserve follow mode unless the scroll position actually moved
            # upward from the last programmatic tail position.
            self._follow_tail = True
        else:
            self._follow_tail = False
        return self._follow_tail

    async def _finish_timeline_update(
        self: LumenApp, container: VerticalScroll, follow_before_update: bool
    ) -> None:
        await self._prune_timeline_widgets(container)
        try:
            indicator = self.query_one("#new-activity", Static)
        except Exception:
            return
        if follow_before_update:
            self._scroll_timeline_end(container)
            indicator.remove_class("visible")
        else:
            indicator.add_class("visible")

    def _scroll_timeline_end(self: LumenApp, container: VerticalScroll) -> None:
        """Follow the tail now and again after Textual's next layout pass."""

        self._tail_follow_generation += 1
        generation = self._tail_follow_generation
        # Textual's native anchor tracks later Rich/Markdown layout growth;
        # repeated scroll_end callbacks alone can run before final measurement.
        container.anchor(True)
        container.scroll_end(animate=False)
        self._last_tail_scroll_y = max(float(container.scroll_y), float(container.scroll_target_y))

        def after_layout() -> None:
            if generation != self._tail_follow_generation or not self._follow_tail:
                return
            reachable_last_tail = min(self._last_tail_scroll_y, float(container.max_scroll_y))
            if float(container.scroll_target_y) < reachable_last_tail:
                self._follow_tail = False
                return
            container.scroll_end(animate=False, force=True, immediate=True)
            self._last_tail_scroll_y = max(float(container.scroll_y), float(container.scroll_target_y))

            def settle_markdown_layout() -> None:
                if generation == self._tail_follow_generation and self._follow_tail:
                    container.scroll_end(animate=False, force=True, immediate=True)

            self.call_after_refresh(settle_markdown_layout)

        self.call_after_refresh(after_layout)

    async def _prune_timeline_widgets(
        self: LumenApp, container: VerticalScroll, *, remove_oldest: bool = True
    ) -> None:
        """Bound mounted widgets while retaining every pending approval."""

        allowed = 202 + len(self._approval_waiters)
        while len(container.children) > allowed:
            candidates = container.children if remove_oldest else reversed(container.children)
            removable = next(
                (
                    child
                    for child in candidates
                    if child is not self._assistant_container
                    and not (isinstance(child, ToolCard) and child.call_id in self._approval_waiters)
                ),
                None,
            )
            if removable is None:
                break
            if isinstance(removable, ToolCard):
                self._tool_cards.pop(removable.call_id, None)
            await removable.remove()

        completed_cards = [
            child
            for child in container.children
            if isinstance(child, ToolCard) and child.call_id not in self._approval_waiters
        ]
        for card in completed_cards[:-20]:
            card.compact()

    async def _load_older_if_at_top(self: LumenApp) -> None:
        """Page older turns when a restored timeline reaches its top."""

        if self._loading_older or self.timeline_store.next_cursor is None:
            return
        messages = self.query_one("#messages", VerticalScroll)
        if messages.max_scroll_y <= 0 or messages.scroll_y > 0:
            return
        self._loading_older = True
        try:
            anchor = messages.children[0] if messages.children else None
            anchor_y = anchor.virtual_region.y if anchor is not None else 0
            page = self.timeline_store.load_older(self.timeline_store.next_cursor, limit=20)
            widgets = [self._timeline_widget(item) for item in page.items]
            widgets = [widget for widget in widgets if widget is not None]
            if widgets:
                if anchor is None:
                    await messages.mount(*widgets)
                else:
                    await messages.mount(*widgets, before=anchor)
                    await self._prune_timeline_widgets(messages, remove_oldest=False)

                    def restore_anchor() -> None:
                        delta = anchor.virtual_region.y - anchor_y
                        messages.scroll_relative(y=delta, animate=False)

                    self.call_after_refresh(restore_anchor)
        finally:
            self._loading_older = False
