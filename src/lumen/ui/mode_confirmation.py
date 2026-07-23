"""Explicit confirmation before entering the fully automatic approval mode."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class AutoModeConfirmation(ModalScreen[bool]):
    DEFAULT_CSS = """
    AutoModeConfirmation {
        align: center middle;
        background: $background 70%;
    }
    AutoModeConfirmation > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    AutoModeConfirmation .title { color: $warning; text-style: bold; }
    AutoModeConfirmation .keys { color: $text-muted; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Enter AUTO approval mode?", classes="title")
            yield Static(
                "Classified read, write, execute, and external tools will run without prompting. "
                "Unknown remote tools still require approval."
            )
            yield Static("Y confirm · N/Esc cancel", classes="keys")

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        key = getattr(event, "key", "")
        if key == "y":
            self.dismiss(True)
        elif key in {"n", "escape"}:
            self.dismiss(False)
        else:
            return
        event.prevent_default()
        event.stop()


__all__ = ["AutoModeConfirmation"]
