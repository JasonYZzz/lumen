"""Fixed, queue-aware approval selector placed directly above the composer."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from lumen.approval import ApprovalPresenter
from lumen.events import ApprovalRequest, ToolApprovalPending


class ApprovalPanel(Vertical):
    """Present one pending tool decision at a time as vertical options.

    Runtime requests may arrive as a sequence. The panel owns their visual
    queue, keeps its position stable above the prompt, and advances to the next
    request after each decision. No option is selected initially.
    """

    can_focus = True

    DEFAULT_CSS = """
    ApprovalPanel {
        display: none;
        height: auto;
        max-height: 18;
        margin: 0 2;
        padding: 0 1;
        background: $surface 92%;
        border-top: solid $warning 55%;
        border-bottom: solid $warning 55%;
    }
    ApprovalPanel.visible { display: block; }
    .approval-title { color: $warning; padding: 0 1; }
    .approval-args { color: $text-muted; padding: 0 3; }
    .approval-option { color: $text-muted; padding: 0 3; }
    .approval-option.selected {
        color: $text;
        background: $primary 28%;
        text-style: bold;
    }
    .approval-keys { color: $text-muted; padding: 0 3; text-style: dim; }
    """

    class Decision(Message):
        def __init__(self, call_id: str, approved: bool) -> None:
            super().__init__()
            self.call_id = call_id
            self.approved = approved

    def __init__(self) -> None:
        super().__init__(id="approval-panel")
        self._pending: list[ToolApprovalPending] = []
        self._selection: int | None = None
        self._decision_posted = False
        self._presenter = ApprovalPresenter()
        self._title = Static("", classes="approval-title", markup=False)
        self._args = Static("", classes="approval-args", markup=False)
        self._allow = Static("  Allow once", classes="approval-option", markup=False)
        self._deny = Static("  Deny", classes="approval-option", markup=False)
        self._keys = Static(
            "↑↓ select · Enter confirm · Esc cancel run",
            classes="approval-keys",
            markup=False,
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._args
        yield self._allow
        yield self._deny
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
            self._selection = None
            self._decision_posted = False
        self.add_class("visible")
        self._refresh()
        self.call_after_refresh(self.focus)  # type: ignore[func-returns-value]

    def resolve(self, call_id: str) -> None:
        was_active = self.active_request is not None and self.active_request.call_id == call_id
        self._pending = [item for item in self._pending if item.call_id != call_id]
        if was_active:
            self._selection = None
            self._decision_posted = False
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
        self.remove_class("visible")
        self._refresh()

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        if self.active_request is None or self._decision_posted:
            return
        key = getattr(event, "key", "")
        if key in {"up", "left", "h"}:
            self._selection = 0
        elif key in {"down", "right", "l"}:
            self._selection = 1
        elif key == "y":
            self._selection = 0
            self._confirm()
        elif key == "n":
            self._selection = 1
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
        self._decision_posted = True
        self.post_message(self.Decision(request.call_id, self._selection == 0))

    def _refresh(self) -> None:
        request = self.active_request
        if request is None:
            self._title.update("")
            self._args.update("")
            self._set_selected(None)
            return
        title = Text("? ")
        title.append(request.name, style="bold")
        title.append(f"  ·  {request.risk}  ·  {request.origin}", style="dim")
        if len(self._pending) > 1:
            title.append(f"  ·  1/{len(self._pending)}", style="dim")
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
        self._args.update(view.preview)
        self._set_selected(self._selection)

    def _set_selected(self, index: int | None) -> None:
        for option_index, widget in enumerate((self._allow, self._deny)):
            selected = option_index == index
            widget.set_class(selected, "selected")
            label = "Allow once" if option_index == 0 else "Deny"
            widget.update(f"> {label}" if selected else f"  {label}")


__all__ = ["ApprovalPanel"]
