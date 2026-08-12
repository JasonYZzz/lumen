"""Compact, expandable transcript blocks for public agent activity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from lumen.ui.activity_indicator import ToolActivityFamily, ToolActivityPresentation
from lumen.ui.themes import theme_color


def _one_line(value: str, *, limit: int = 96) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class CommentaryBlock(Vertical):
    """Public reasoning summary which can be expanded without exposing hidden CoT."""

    can_focus = True
    DEFAULT_CSS = """
    CommentaryBlock {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        color: $text-muted;
    }
    CommentaryBlock:focus { background: $surface 45%; }
    .commentary-summary { color: $text-muted; text-style: italic; }
    .commentary-block { display: none; padding: 0 2; color: $text-muted; }
    CommentaryBlock.expanded .commentary-block { display: block; }
    CommentaryBlock.expanded .commentary-summary { text-style: bold; }
    """

    def __init__(self, *, expanded: bool = False) -> None:
        super().__init__()
        self._text = ""
        self._expanded = expanded
        self._summary = Static("", classes="commentary-summary", markup=False)
        self._body = Static("", classes="commentary-block", markup=False)

    def compose(self) -> ComposeResult:
        yield self._summary
        yield self._body

    def on_mount(self) -> None:
        self.set_class(self._expanded, "expanded")
        self._refresh()

    @property
    def text(self) -> str:
        return self._text

    @property
    def expanded(self) -> bool:
        return self._expanded

    def append(self, text: str) -> None:
        self._text += text
        self._refresh()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.set_class(expanded, "expanded")
        self._refresh()

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        if getattr(event, "key", "") not in {"enter", "e", "space"}:
            return
        self.set_expanded(not self._expanded)
        event.prevent_default()
        event.stop()

    @on(events.Click, ".commentary-summary")
    def toggle_from_click(self, event: events.Click) -> None:
        self.set_expanded(not self._expanded)
        event.stop()

    def _refresh(self) -> None:
        marker = "▾" if self._expanded else "▸"
        self._summary.update(f"{marker} Thinking · {_one_line(self._text) or 'working…'}")
        # Keep this exact prefix for stable transcript export and compatibility.
        self._body.update(f"∴ {self._text}")


@dataclass(slots=True)
class _ReadCall:
    name: str
    presentation: ToolActivityPresentation
    status: str = "running"


class ReadToolGroup(Vertical):
    """Collapse compatible low-risk activity without erasing its visible intent."""

    can_focus = True
    DEFAULT_CSS = """
    ReadToolGroup { height: auto; margin: 1 1; padding: 0 1; color: $text-muted; }
    ReadToolGroup:focus { background: $surface 45%; }
    .read-group-header { color: $text-muted; }
    ReadToolGroup.running .read-group-header { color: $secondary; text-style: bold; }
    ReadToolGroup.failed .read-group-header { color: $error; text-style: bold; }
    ReadToolGroup:hover { background: $secondary 6%; }
    .read-group-body { display: none; padding: 0 2; color: $text-muted; }
    ReadToolGroup.expanded .read-group-body { display: block; }
    """

    def __init__(self, *, expanded: bool = False) -> None:
        super().__init__()
        self._calls: dict[str, _ReadCall] = {}
        self._group_key: str | None = None
        self._expanded = expanded
        self._header = Static("", classes="read-group-header", markup=False)
        self._body = Static("", classes="read-group-body", markup=False)

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def on_mount(self) -> None:
        self.set_class(self._expanded, "expanded")
        self._refresh()

    @property
    def group_key(self) -> str | None:
        return self._group_key

    def is_compatible(self, presentation: ToolActivityPresentation) -> bool:
        return bool(presentation.groupable and self._group_key == presentation.group_key)

    def start_call(
        self,
        call_id: str,
        name: str,
        presentation: ToolActivityPresentation,
    ) -> None:
        if self._group_key is None:
            self._group_key = presentation.group_key
        self._calls[call_id] = _ReadCall(name=name, presentation=presentation)
        self._refresh()

    def finish_call(self, call_id: str, *, is_error: bool) -> None:
        call = self._calls.get(call_id)
        if call is not None:
            call.status = "error" if is_error else "ok"
            if is_error:
                self.set_expanded(True)
            self._refresh()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.set_class(expanded, "expanded")
        self._refresh()

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        if getattr(event, "key", "") not in {"enter", "e", "space"}:
            return
        self._expanded = not self._expanded
        self.set_class(self._expanded, "expanded")
        self._refresh()
        event.prevent_default()
        event.stop()

    @on(events.Click, ".read-group-header")
    def toggle_from_click(self, event: events.Click) -> None:
        self.set_expanded(not self._expanded)
        event.stop()

    def _refresh(self) -> None:
        total = len(self._calls)
        running = sum(call.status == "running" for call in self._calls.values())
        errors = sum(call.status == "error" for call in self._calls.values())
        marker = "▾" if self._expanded else "▸"
        self.set_class(bool(running), "running")
        self.set_class(bool(errors), "failed")
        latest = self._calls[next(reversed(self._calls))] if self._calls else None
        summary = self._summary(running=running)
        # Present-tense verbs already communicate liveness; repeating
        # "1 active" adds noise without new information.
        state = f" · {errors} failed" if errors else ""
        detail = latest.presentation.detail if latest is not None else None
        suffix = f" · {_one_line(detail, limit=48)}" if total == 1 and detail else ""
        header = Text(f"{marker} ", style=self._color("activity-meta", "#948A80"))
        if errors:
            summary_color = self._color("error", "#D16D75")
        elif running:
            summary_color = self._color("tool", "#C7ACE8")
        else:
            summary_color = self._color("foreground", "#ECE9E4")
        header.append(summary, style=f"bold {summary_color}" if running or errors else summary_color)
        header.append(state + suffix, style=self._color("activity-meta", "#948A80"))
        self._header.update(header)
        body = Text()
        for index, call in enumerate(self._calls.values()):
            if index:
                body.append("\n")
            glyph = {"running": "●", "ok": "✓", "error": "✗"}[call.status]
            glyph_color = {
                "running": self._color("tool", "#C7ACE8"),
                "ok": self._color("success", "#86A66C"),
                "error": self._color("error", "#D16D75"),
            }[call.status]
            presentation = call.presentation
            label = (
                presentation.active_verb
                if call.status == "running"
                else presentation.completed_verb
            )
            detail = presentation.detail
            body.append(f"{glyph} ", style=f"bold {glyph_color}")
            body.append(label, style=self._color("foreground", "#ECE9E4"))
            if detail:
                body.append(f" · {detail}", style=self._color("activity-meta", "#948A80"))
        self._body.update(body)

    def _summary(self, *, running: int) -> str:
        calls = tuple(self._calls.values())
        total = len(calls)
        families = {call.presentation.family for call in calls}
        active = running > 0
        if families == {ToolActivityFamily.READ}:
            return f"{'Reading' if active else 'Read'} {total} {_unit(total, 'file', 'files')}"
        if families == {ToolActivityFamily.LIST}:
            return (
                f"{'Inspecting' if active else 'Inspected'} {total} "
                f"{_unit(total, 'directory', 'directories')}"
            )
        if families <= {ToolActivityFamily.READ, ToolActivityFamily.LIST}:
            return f"{'Exploring' if active else 'Explored'} {total} {_unit(total, 'item', 'items')}"
        if families == {ToolActivityFamily.SEARCH}:
            return f"{'Searching' if active else 'Searched'} {total} {_unit(total, 'pattern', 'patterns')}"
        if families == {ToolActivityFamily.WEB}:
            return (
                f"{'Searching' if active else 'Searched'} the web · {total} "
                f"{_unit(total, 'search', 'searches')}"
            )
        if len(families) == 1 and calls:
            presentation = calls[0].presentation
            verb = presentation.active_verb if active else presentation.completed_verb
            return f"{verb} · {total} {_unit(total, presentation.singular, presentation.plural)}"
        return f"{'Working with' if active else 'Used'} {total} {_unit(total, 'tool', 'tools')}"

    def _color(self, token: str, fallback: str) -> str:
        if not self.is_mounted:
            return fallback
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        return theme_color(app, token, fallback)


def _unit(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


__all__ = ["CommentaryBlock", "ReadToolGroup"]
