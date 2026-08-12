"""Reverse prompt-history search overlay."""

from __future__ import annotations

from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class HistorySearchScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    HistorySearchScreen { align: center bottom; background: $background 18%; }
    #history-shell {
        width: 88%; max-width: 96; height: auto; max-height: 16;
        margin-bottom: 5; padding: 1 2;
        background: $surface; border: tall $primary 65%;
    }
    #history-title { height: 1; color: $text; text-style: bold; }
    #history-query { height: 3; }
    #history-results { height: auto; color: $text-muted; }
    """

    def __init__(self, entries: list[str]) -> None:
        super().__init__()
        self._entries = list(reversed(entries))
        self._matches = self._entries[:8]
        self._selection = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="history-shell"):
            yield Static(
                "Reverse history search · ↑↓ choose · Enter restore",
                id="history-title",
                markup=False,
            )
            yield Input(placeholder="Search previous prompts…", id="history-query")
            yield Static("", id="history-results", markup=False)

    def on_mount(self) -> None:
        self._refresh()
        self.query_one(Input).focus()

    @on(Input.Changed)
    def changed(self, event: Input.Changed) -> None:
        query = event.value.casefold().strip()
        self._matches = [item for item in self._entries if query in item.casefold()][:8]
        self._selection = 0
        self._refresh()

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        key = getattr(event, "key", "")
        if key == "up" and self._matches:
            self._selection = (self._selection - 1) % len(self._matches)
        elif key == "down" and self._matches:
            self._selection = (self._selection + 1) % len(self._matches)
        elif key == "enter":
            self.dismiss(self._matches[self._selection] if self._matches else None)
        else:
            return
        self._refresh()
        event.prevent_default()
        event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _refresh(self) -> None:
        lines: list[str] = []
        for index, item in enumerate(self._matches):
            marker = ">" if index == self._selection else " "
            value = " ".join(item.split())
            lines.append(f"{marker} {value[:120]}" + ("…" if len(value) > 120 else ""))
        self.query_one("#history-results", Static).update("\n".join(lines) or "No matching prompts")


__all__ = ["HistorySearchScreen"]
