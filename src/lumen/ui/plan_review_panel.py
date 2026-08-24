"""Composer-adjacent review gate for turning a read-only plan into execution."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

_CURSOR = "❯"  # noqa: RUF001 - matches the other numbered TUI selectors


class PlanReviewPanel(Vertical):
    """Offer the execution modes that complete the Plan-mode lifecycle."""

    can_focus = True

    DEFAULT_CSS = """
    PlanReviewPanel {
        display: none;
        height: auto;
        margin: 0 2;
        padding: 0 1 1 1;
        background: $success 4%;
    }
    PlanReviewPanel.visible { display: block; }
    .plan-review-title { color: $text; padding: 0 1; text-style: bold; }
    .plan-review-summary { color: $text-muted; padding: 0 3 1 3; }
    .plan-review-option { color: $text-muted; padding: 0 2; }
    .plan-review-option.selected {
        color: $text;
        background: $success 12%;
        text-style: bold;
    }
    .plan-review-keys { color: $text-muted; padding: 0 3; }
    """

    class Decision(Message):
        def __init__(self, mode: str | None) -> None:
            super().__init__()
            self.mode = mode

    class EditRequested(Message):
        pass

    _OPTIONS: tuple[tuple[str, str | None], ...] = (
        ("Approve and start in Auto", "auto"),
        ("Approve and accept edits", "accept_edits"),
        ("Approve and review each action", "manual"),
        ("Keep planning with feedback", None),
    )

    def __init__(self) -> None:
        super().__init__(id="plan-review-panel")
        self._selection = 0
        self._decision_posted = False
        self._confirming_auto = False
        self._title = Static("Plan ready", classes="plan-review-title", markup=False)
        self._summary = Static("", classes="plan-review-summary", markup=False)
        self._options = [
            Static("", classes="plan-review-option", markup=False) for _ in self._OPTIONS
        ]
        self._keys = Static(
            "Enter select · ↑↓ move · 1-4 choose · Esc keep planning",
            classes="plan-review-keys",
            markup=False,
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._summary
        yield from self._options
        yield self._keys

    def show(
        self, *, step_count: int, current_mode: str = "manual", revision: int | None = None
    ) -> None:
        mode_to_index = {mode: index for index, (_, mode) in enumerate(self._OPTIONS)}
        self._selection = mode_to_index.get(current_mode, 2)
        self._decision_posted = False
        self._confirming_auto = False
        if step_count:
            noun = "step" if step_count == 1 else "steps"
            summary = f"{step_count} {noun} proposed"
        else:
            summary = "Proposal ready"
        revision_text = f" · revision {revision}" if revision is not None else ""
        self._summary.update(
            f"{summary}{revision_text} · choose how changes should be approved"
        )
        self.add_class("visible")
        self._refresh()
        self.call_after_refresh(self.focus)  # type: ignore[func-returns-value]

    def hide(self) -> None:
        self._decision_posted = False
        self._confirming_auto = False
        self.remove_class("visible")

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        if not self.has_class("visible") or self._decision_posted:
            return
        key = getattr(event, "key", "")
        if key in {"up", "left", "h"}:
            self._confirming_auto = False
            self._selection = (self._selection - 1) % len(self._OPTIONS)
        elif key in {"down", "right", "l"}:
            self._confirming_auto = False
            self._selection = (self._selection + 1) % len(self._OPTIONS)
        elif key in {"1", "2", "3", "4"}:
            self._confirming_auto = False
            self._selection = int(key) - 1
            self._confirm()
        elif key == "enter":
            self._confirm()
        elif key == "escape":
            if self._confirming_auto:
                self._confirming_auto = False
            else:
                self._selection = 3
                self._confirm()
        elif key == "ctrl+g":
            self.post_message(self.EditRequested())
        else:
            return
        self._refresh()
        event.prevent_default()
        event.stop()

    def _confirm(self) -> None:
        if self._decision_posted:
            return
        if self._OPTIONS[self._selection][1] == "auto" and not self._confirming_auto:
            self._confirming_auto = True
            self._summary.update(
                "Auto runs classified edits and commands without per-action review · Enter again to approve"
            )
            return
        self._decision_posted = True
        self.post_message(self.Decision(self._OPTIONS[self._selection][1]))

    def _refresh(self) -> None:
        for index, (label, _) in enumerate(self._OPTIONS):
            selected = index == self._selection
            option = self._options[index]
            option.set_class(selected, "selected")
            marker = _CURSOR if selected else " "
            option.update(f"{marker} {index + 1}. {label}")
        self._keys.update(
            "Enter again confirms Auto · Esc cancels confirmation"
            if self._confirming_auto
            else "Enter select · ↑↓ move · 1-4 choose · Ctrl+G edit · Esc keep planning"
        )


__all__ = ["PlanReviewPanel"]
