"""Searchable raw/concise transcript overlay."""

from __future__ import annotations

import json
from typing import Any, ClassVar, cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea

from lumen.timeline import TimelineItem, TimelineKind


class TranscriptScreen(ModalScreen[None]):
    """Inspect, search and copy the structured transcript without leaving the run."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("alt+r", "toggle_raw", "Raw"),
        Binding("alt+e", "toggle_expanded", "Expand all"),
        Binding("alt+c", "copy", "Copy"),
        Binding("ctrl+n", "next_match", "Next"),
        Binding("ctrl+p", "previous_match", "Previous"),
        Binding("ctrl+down", "next_turn", "Next turn"),
        Binding("ctrl+up", "previous_turn", "Previous turn"),
    ]
    CSS = """
    TranscriptScreen { align: center middle; background: $background; }
    #transcript-shell {
        width: 100%; height: 100%; padding: 1 2;
        background: $background; border: none;
    }
    #transcript-title { height: 1; color: $text; text-style: bold; }
    #transcript-hint { height: 1; color: $text-muted; }
    #transcript-search { height: 3; margin: 1 0 0 0; }
    #transcript-body { height: 1fr; border: tall $panel; background: $surface; }
    """

    def __init__(self, items: tuple[TimelineItem, ...], *, raw: bool = False) -> None:
        super().__init__()
        self._items = items
        self._raw = raw
        self._expanded = False
        self._matches: list[int] = []
        self._match_index = -1
        self._turn_rows: list[int] = []
        self._turn_index = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="transcript-shell"):
            yield Static("Transcript", id="transcript-title", markup=False)
            yield Static(
                "Ctrl+F search · Ctrl+N/P matches · Alt+E expand · Alt+R raw · Alt+C copy",
                id="transcript-hint",
                markup=False,
            )
            yield Input(placeholder="Search transcript…", id="transcript-search")
            yield TextArea("", id="transcript-body", read_only=True, show_line_numbers=False)

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#transcript-search", Input).focus()

    @on(Input.Changed, "#transcript-search")
    def search_changed(self, event: Input.Changed) -> None:
        self._refresh(query=event.value)

    def action_close(self) -> None:
        self.dismiss()

    def action_focus_search(self) -> None:
        self.query_one("#transcript-search", Input).focus()

    def action_toggle_raw(self) -> None:
        self._raw = not self._raw
        self._refresh()

    def action_toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._refresh()

    def action_copy(self) -> None:
        cast(Any, self.app).copy_to_clipboard(  # type: ignore[reportUnknownMemberType]
            self.query_one("#transcript-body", TextArea).text
        )
        self.notify("Transcript copied", timeout=2)

    def action_next_match(self) -> None:
        self._move_match(1)

    def action_previous_match(self) -> None:
        self._move_match(-1)

    def action_next_turn(self) -> None:
        self._move_turn(1)

    def action_previous_turn(self) -> None:
        self._move_turn(-1)

    def _move_match(self, direction: int) -> None:
        if not self._matches:
            return
        self._match_index = (self._match_index + direction) % len(self._matches)
        row = self._matches[self._match_index]
        body = self.query_one("#transcript-body", TextArea)
        body.cursor_location = (row, 0)
        body.scroll_cursor_visible(animate=False)

    def _move_turn(self, direction: int) -> None:
        if not self._turn_rows:
            return
        self._turn_index = (self._turn_index + direction) % len(self._turn_rows)
        body = self.query_one("#transcript-body", TextArea)
        body.cursor_location = (self._turn_rows[self._turn_index], 0)
        body.scroll_cursor_visible(animate=False)

    def _refresh(self, *, query: str | None = None) -> None:
        body = self.query_one("#transcript-body", TextArea)
        current_query = query if query is not None else self.query_one("#transcript-search", Input).value
        rendered = self._render_items()
        body.text = rendered
        lines = rendered.splitlines()
        self._turn_rows = [
            index
            for index, line in enumerate(lines)
            if line.startswith("[USER]")
            or ('"kind": "user"' in line and line.lstrip().startswith("{"))
        ]
        self._turn_index = -1
        needle = current_query.casefold().strip()
        self._matches = [
            index for index, line in enumerate(lines) if needle and needle in line.casefold()
        ]
        self._match_index = -1
        mode = "raw JSON" if self._raw else ("expanded" if self._expanded else "concise")
        match = f" · {len(self._matches)} matches" if needle else ""
        self.query_one("#transcript-title", Static).update(
            f"Transcript · {len(self._items)} events · {mode}{match}"
        )

    def _render_items(self) -> str:
        if self._raw:
            return "\n".join(json.dumps(_item_payload(item), ensure_ascii=False) for item in self._items)
        rows: list[str] = []
        for item in self._items:
            label = item.kind.value.upper()
            if item.kind is TimelineKind.TOOL:
                detail = f"{item.tool_name or 'tool'} [{item.status or 'unknown'}]"
                if self._expanded:
                    detail += "\n" + json.dumps(item.args or {}, ensure_ascii=False, indent=2)
                    if item.result:
                        detail += "\n" + item.result
            elif item.kind is TimelineKind.PLAN:
                detail = json.dumps(item.plan or {}, ensure_ascii=False, indent=2 if self._expanded else None)
            else:
                detail = item.text
                if item.kind is TimelineKind.COMMENTARY and not self._expanded:
                    detail = " ".join(detail.split())[:160]
            rows.append(f"[{label}] {detail}".rstrip())
        return "\n\n".join(rows)


def _item_payload(item: TimelineItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind.value,
        "text": item.text,
        "call_id": item.call_id,
        "tool_name": item.tool_name,
        "args": item.args,
        "result": item.result,
        "preview": item.preview,
        "status": item.status,
        "pending_approval": item.pending_approval,
        "is_error": item.is_error,
        "plan": item.plan,
    }


__all__ = ["TranscriptScreen"]
