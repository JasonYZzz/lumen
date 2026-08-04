"""Compact, queue-aware permission selector placed above the composer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from lumen.approval import ApprovalPresenter, ApprovalViewModel
from lumen.events import ApprovalRequest, ToolApprovalBatchPending, ToolApprovalPending
from lumen.ui.diff_view import style_diff_text
from lumen.ui.themes import theme_color

_CURSOR = "❯"  # noqa: RUF001 - matches the established terminal list cursor

#: Tools whose approval preview is a unified diff and therefore gets the same
#: red/green coloring as the timeline tool card.
_DIFF_PREVIEW_TOOLS = frozenset({"edit_file", "write_file"})


class ApprovalPanel(Vertical):
    """Present one pending tool decision at a time as vertical options.

    Runtime requests may arrive as a sequence. The panel owns their visual
    queue, keeps its position stable above the prompt, and advances to the next
    request after each decision. The first option is selected, matching the
    numbered-list interaction used by Claude Code.
    """

    can_focus = True

    DEFAULT_CSS = """
    ApprovalPanel {
        display: none;
        height: auto;
        max-height: 18;
        margin: 0 2;
        padding: 0 1 1 1;
        background: $warning 4%;
    }
    ApprovalPanel.visible { display: block; }
    .approval-title { color: $text; padding: 0 1; }
    .approval-args { color: $text-muted; padding: 0 3 1 3; }
    .approval-option { color: $text-muted; padding: 0 2; }
    .approval-option.selected {
        color: $text;
        background: $warning 12%;
        text-style: bold;
    }
    .approval-keys { color: $text-muted; padding: 0 3; }
    """

    class Decision(Message):
        def __init__(self, call_id: str, approved: bool, *, remember: bool = False) -> None:
            super().__init__()
            self.call_id = call_id
            self.approved = approved
            self.remember = remember

    class BatchDecision(Message):
        def __init__(self, call_ids: tuple[str, ...], approved: bool) -> None:
            super().__init__()
            self.call_ids = call_ids
            self.approved = approved

    def __init__(self, *, workspace: str | Path | None = None) -> None:
        super().__init__(id="approval-panel")
        self._pending: list[ToolApprovalPending] = []
        self._selection: int | None = None
        self._decision_posted = False
        self._batch_mode = False
        self._presenter = ApprovalPresenter(workspace_root=workspace)
        self._title = Static("", classes="approval-title", markup=False)
        self._args = Static("", classes="approval-args", markup=False)
        self._allow = Static(f"{_CURSOR} 1. Allow once", classes="approval-option", markup=False)
        self._deny = Static("  2. Always allow for this session", classes="approval-option", markup=False)
        self._deny_all = Static("  3. Deny", classes="approval-option", markup=False)
        self._keys = Static(
            "Enter select · ↑↓ move · Esc cancel",
            classes="approval-keys",
            markup=False,
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._args
        yield self._allow
        yield self._deny
        yield self._deny_all
        yield self._keys

    @property
    def active_request(self) -> ToolApprovalPending | None:
        return self._pending[0] if self._pending else None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pending_requests(self) -> tuple[ToolApprovalPending, ...]:
        """Stable public view used when Auto mode resolves the visible queue."""

        return tuple(self._pending)

    def enqueue(self, request: ToolApprovalPending) -> None:
        if any(item.call_id == request.call_id for item in self._pending):
            return
        self._pending.append(request)
        if len(self._pending) == 1:
            self._selection = 0
            self._decision_posted = False
        self.add_class("visible")
        self._refresh()
        self.call_after_refresh(self.focus)  # type: ignore[func-returns-value]

    def enqueue_batch(self, batch: ToolApprovalBatchPending) -> None:
        for request in batch.requests:
            if any(item.call_id == request.call_id for item in self._pending):
                continue
            self._pending.append(
                ToolApprovalPending(
                    request.call_id,
                    request.name,
                    request.args,
                    request.origin,
                    request.risk,
                )
            )
        if not self._pending:
            return
        self._batch_mode = len(self._pending) > 1
        self._selection = 0
        self._decision_posted = False
        self.add_class("visible")
        self._refresh()
        self.call_after_refresh(self.focus)  # type: ignore[func-returns-value]

    def resolve(self, call_id: str) -> None:
        was_active = self.active_request is not None and self.active_request.call_id == call_id
        self._pending = [item for item in self._pending if item.call_id != call_id]
        if was_active:
            self._selection = 0
            self._decision_posted = False
        if len(self._pending) <= 1:
            self._batch_mode = False
        if self._pending:
            self.add_class("visible")
            self._refresh()
            self.call_after_refresh(self.focus)  # type: ignore[func-returns-value]
        else:
            self.remove_class("visible")
            self._refresh()

    def clear(self) -> None:
        self._pending.clear()
        self._selection = None
        self._decision_posted = False
        self._batch_mode = False
        self.remove_class("visible")
        self._refresh()

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        if self.active_request is None or self._decision_posted:
            return
        key = getattr(event, "key", "")
        option_count = 3
        if key in {"up", "left", "h"}:
            self._selection = (
                option_count - 1
                if self._batch_mode and self._selection is None
                else 0
                if self._selection is None
                else (self._selection - 1) % option_count
            )
        elif key in {"down", "right", "l"}:
            self._selection = (
                0
                if self._batch_mode and self._selection is None
                else 1
                if self._selection is None
                else (self._selection + 1) % option_count
            )
        elif key in {"1", "y"}:
            self._selection = 0
            self._confirm()
        elif key == "2":
            self._selection = 1
            self._confirm()
        elif key == "3":
            self._selection = 2
            self._confirm()
        elif key == "n":
            self._selection = 2
            self._confirm()
        elif key == "enter":
            self._confirm()
        else:
            return
        self._refresh()
        event.prevent_default()
        event.stop()

    def _confirm(self) -> None:
        request = self.active_request
        if request is None or self._selection is None or self._decision_posted:
            return
        if self._batch_mode:
            if self._selection == 1:
                self._batch_mode = False
                self._selection = 0
                self._refresh()
                return
            self._decision_posted = True
            self.post_message(
                self.BatchDecision(
                    tuple(item.call_id for item in self._pending),
                    self._selection == 0,
                )
            )
            return
        self._decision_posted = True
        self.post_message(
            self.Decision(
                request.call_id,
                self._selection in {0, 1},
                remember=self._selection == 1,
            )
        )

    def _refresh(self) -> None:
        request = self.active_request
        if request is None:
            self._title.update("")
            self._args.update("")
            self._set_selected(None)
            return
        title = Text(f"{_CURSOR} ", style="bold")
        title.append(
            (
                f"Allow {len(self._pending)} tool calls?"
                if self._batch_mode
                else f"Allow {request.name}?"
            ),
            style="bold",
        )
        title.append(
            f"  {request.risk} · {request.origin}" if not self._batch_mode else "",
            style="dim",
        )
        if len(self._pending) > 1:
            title.append(f"  1 of {len(self._pending)}", style="dim")
        self._title.update(title)
        view = self._presenter.build(
            ApprovalRequest(
                call_id=request.call_id,
                name=request.name,
                args=request.args,
                origin=request.origin,
                risk=request.risk,
            )
        )
        if self._batch_mode:
            rows = [f"· {item.name}" for item in self._pending[:4]]
            if len(self._pending) > 4:
                rows.append(f"· {len(self._pending) - 4} more")
            self._args.update("\n".join(rows))
        else:
            self._args.update(self._render_preview(request, view))
        self._set_selected(self._selection)

    def _render_preview(self, request: ToolApprovalPending, view: ApprovalViewModel) -> Text | str:
        """Build the args preview, coloring diffs like the timeline tool card.

        The compact line/char budget is applied to the raw string first so
        styling never changes how much content fits on short terminals.
        """

        clipped = _compact_preview(view.preview)
        if request.name not in _DIFF_PREVIEW_TOOLS:
            return clipped
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        return style_diff_text(
            clipped,
            add_color=theme_color(app, "success", "green"),
            del_color=theme_color(app, "error", "red"),
        )

    def _set_selected(self, index: int | None) -> None:
        widgets = (self._allow, self._deny, self._deny_all)
        labels = (
            ("Allow all", "Review individually", "Deny all")
            if self._batch_mode
            else ("Allow once", "Always allow for this session", "Deny")
        )
        for option_index, widget in enumerate(widgets):
            selected = option_index == index
            widget.set_class(selected, "selected")
            label = labels[option_index]
            widget.display = bool(label)
            marker = _CURSOR if selected else " "
            widget.update(f"{marker} {option_index + 1}. {label}")


def _compact_preview(value: str, *, max_lines: int = 6, max_chars: int = 900) -> str:
    """Keep the decision itself visible on short terminals."""

    lines = value.splitlines()
    clipped = "\n".join(lines[:max_lines])[:max_chars]
    if len(lines) > max_lines or len(value) > max_chars:
        return clipped.rstrip() + "\n…"
    return clipped


__all__ = ["ApprovalPanel"]
