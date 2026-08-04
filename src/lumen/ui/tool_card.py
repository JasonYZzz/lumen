"""Timeline tool card: compact, expandable tool lifecycle audit.

A card is created when a tool call starts and updated as it finishes or as an
approval decision becomes pending/resolved. The application-level
``ApprovalPanel`` owns production interaction above the composer; cards remain
an ordered audit trail. A standalone selector is retained for isolated reuse
and follows the same vertical arrow-key vocabulary.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from lumen.events import ToolApprovalPending
from lumen.ui.diff_view import style_diff_lines, unified_diff_lines
from lumen.ui.themes import theme_color

_STATUS_GLYPHS = {
    "running": "●",
    "ok": "✓",
    "error": "✗",
    "denied": "⊘",
    "approved": "✓",
    "pending": "?",
}

#: The two options shown in the approval selector. No option is selected until
#: the user deliberately navigates with an arrow key.
_APPROVAL_OPTIONS = ("Allow", "Deny")
_COLLAPSED_ARG_CHARS = 800


class ToolCard(Vertical):
    """A single tool call rendered as a timeline card."""

    # Vertical containers are not focusable by default. We opt in so the
    # approval selector can receive keyboard input (Up/Down/Enter) when
    # the card is pending. Without this, self.focus() silently no-ops and
    # keypresses never reach on_key.
    can_focus = True

    DEFAULT_CSS = """
    ToolCard {
        height: auto;
        margin: 1 1;
        padding: 0 1 0 1;
        background: $background;
    }
    ToolCard.is-error {
        background: $error 5%;
    }
    ToolCard.is-pending {
        background: $warning 5%;
    }
    .tool-header { color: $text; }
    .tool-args {
        color: $text-muted;
        padding: 0 2;
        background: $background;
    }
    .tool-result { padding: 0 2; color: $text-muted; }
    .tool-result.is-error { color: $error; }
    /* Approval row: amber, bold to draw the eye to the pending decision. */
    .tool-approval {
        height: auto;
        padding: 0 1;
        color: $warning;
        text-style: bold;
    }
    .tool-approval-hint {
        color: $text-muted;
        text-style: italic;
        padding: 0 1;
    }
    """

    class Decision(Message):
        """Posted when the user confirms Allow or Deny on the card."""

        def __init__(self, call_id: str, approved: bool) -> None:
            super().__init__()
            self.call_id = call_id
            self.approved = approved

    #: Keys that drive the approval selector when the card has focus.
    _APPROVAL_KEYS: ClassVar[set[str]] = {
        "left",
        "right",
        "h",
        "l",
        "y",
        "n",
        "enter",
        "tab",
    }

    def __init__(self, call_id: str, tool_name: str) -> None:
        super().__init__()
        self.call_id = call_id
        # Textual widgets already expose ``name``; we use ``tool_name`` for the
        # model-visible tool being rendered on this card.
        self.tool_name = tool_name
        self._status = "running"
        self._risk: str | None = None
        self._origin: str | None = None
        self._elapsed_seconds: float | None = None
        self._exit_code: int | None = None
        self._args: dict[str, Any] | None = None
        self._result: str | None = None
        self._preview: str | None = None
        self._expanded = False
        self._compact = False
        # All three widgets render model/tool output that can contain
        # markup-unsafe characters (``key: value``, ``[...]``, etc.), so we
        # disable rich-markup parsing on each.
        self._header = Static("", classes="tool-header", markup=False)
        self._body = Static("", classes="tool-args", markup=False)
        self._result_widget = Static("", classes="tool-result", markup=False)
        # Approval selector state. ``_approval_index`` is the highlighted
        # option (0=Allow, 1=Deny). The selector widget uses markup=True so we
        # can color the active option via [$accent]...[/] tags; the option
        # labels themselves are hardcoded (no user content), so markup is safe.
        self._approval_label: Static | None = None
        self._approval_hint: Static | None = None
        self._approval_index: int | None = None
        self._decided = False

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body
        yield self._result_widget

    def on_mount(self) -> None:
        self._refresh_header()
        self._refresh_body()

    def start(
        self,
        *,
        args: dict[str, Any],
        origin: str,
        risk: str,
        started_at: float = 0.0,
    ) -> None:
        self._args = args
        self._origin = origin
        self._risk = risk
        self._status = "running"
        self._refresh_header()
        self._refresh_body()

    def update_result(
        self,
        *,
        result: str,
        preview: str | None = None,
        is_error: bool,
        elapsed_seconds: float = 0.0,
        exit_code: int | None = None,
    ) -> None:
        self._result = result
        self._preview = preview
        self._elapsed_seconds = elapsed_seconds
        self._exit_code = exit_code
        self._status = "error" if is_error else "ok"
        self._refresh_result()
        self._result_widget.set_class(is_error, "is-error")
        if is_error:
            self.add_class("is-error")
        else:
            self.remove_class("is-error")
        # A finished card no longer needs its approval selector; the runtime
        # has already resolved the decision.
        self._remove_approval_widgets()
        self._refresh_header()

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def displayed_args(self) -> str:
        return str(getattr(self._body, "content", ""))

    @property
    def displayed_result(self) -> str:
        return str(getattr(self._result_widget, "content", ""))

    def compact(self) -> None:
        """Collapse a completed historical card to its one-line header."""

        if self._status in {"ok", "error", "approved", "denied"}:
            self._compact = True
            self._refresh_body()
            self._refresh_result()

    def set_approval_pending(self, request: ToolApprovalPending) -> None:
        self._status = "pending"
        self._args = request.args
        self.add_class("is-pending")
        self._refresh_header()
        self._refresh_body()
        # Mount the inline approval selector. We use markup=True here because
        # the rendered string is built from our own [$accent]...[/] tags around
        # the hardcoded option labels — no user-supplied content reaches it.
        if self._approval_label is None:
            self._approval_index = None
            self._approval_label = Static(self._render_selector(), classes="tool-approval")
            self._approval_hint = Static(
                "↑↓ select · Enter confirm",
                classes="tool-approval-hint",
                markup=False,
            )
            self.mount(self._approval_label)
            self.mount(self._approval_hint)
            # Take focus so keypresses route to this card's on_key. We defer
            # via call_after_refresh because the just-mounted widgets need a
            # layout pass before focus can land; calling self.focus() inline
            # silently no-ops. The app returns focus to the prompt editor once
            # the decision resolves (see _handle_tool_decision).
            self.call_after_refresh(self.focus)  # type: ignore[func-returns-value]

    def mark_approval_pending(self, request: ToolApprovalPending) -> None:
        """Mark the timeline record pending without mounting controls.

        The application-level :class:`ApprovalPanel` owns the interactive
        selector so multiple decisions do not scatter focusable blocks through
        the transcript. This method keeps the audit trail compact.
        """

        self._status = "pending"
        self._args = request.args
        # The stable composer-adjacent ApprovalPanel owns the full preview.
        # Keep only a one-line audit marker in the timeline to avoid rendering
        # the same command or file content twice.
        self._compact = True
        self.add_class("is-pending")
        self._refresh_header()
        self._refresh_body()

    def resolve_approval(self, *, approved: bool) -> None:
        self._decided = True
        self._status = "approved" if approved else "denied"
        self.remove_class("is-pending")
        self._remove_approval_widgets()
        self._refresh_header()

    # -- approval selector keyboard navigation -----------------------------

    def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        """Drive the approval selector when the card is focused and pending.

        Up/Down move the highlight between Allow and Deny; Enter confirms.
        We only intercept these keys while the selector is
        actually mounted — once resolved, all keys fall through normally.
        """

        key = getattr(event, "key", "")
        if key == "e" and self._approval_label is None and self._status in {"ok", "error"}:
            self._expanded = not self._expanded
            self._compact = False
            self._refresh_body()
            self._refresh_result()
            event.prevent_default()
            event.stop()
            return
        if self._decided or self._approval_label is None:
            return
        if key == "up":
            self._approval_index = 0
            self._refresh_selector()
            event.prevent_default()
            event.stop()
        elif key == "down":
            self._approval_index = 1
            self._refresh_selector()
            event.prevent_default()
            event.stop()
        elif key == "enter":
            self._confirm_selection()
            event.prevent_default()
            event.stop()

    def _render_selector(self) -> str:
        """Build the ``→ Allow    Deny`` line with the active option accented.

        Uses Textual markup (``[...]`` tags) to color the highlighted option.
        Safe because the option labels are hardcoded, not user content.
        """

        parts: list[str] = []
        for i, opt in enumerate(_APPROVAL_OPTIONS):
            if self._approval_index is not None and i == self._approval_index:
                parts.append(f"[$accent on $surface]→ {opt}[/]")
            else:
                parts.append(f"[dim]  {opt}[/]")
        return "    ".join(parts)

    def _refresh_selector(self) -> None:
        if self._approval_label is not None:
            self._approval_label.update(self._render_selector())

    def _confirm_selection(self) -> None:
        if self._decided or self._approval_index is None:
            return
        self._decided = True
        approved = self._approval_index == 0
        self.post_message(self.Decision(self.call_id, approved))

    def _remove_approval_widgets(self) -> None:
        for widget in (self._approval_label, self._approval_hint):
            if widget is not None:
                widget.remove()
        self._approval_label = None
        self._approval_hint = None

    def _refresh_header(self) -> None:
        glyph = _STATUS_GLYPHS.get(self._status, "●")
        meta_bits: list[str] = []
        if self._risk:
            meta_bits.append(self._risk)
        if self._origin:
            meta_bits.append(self._origin)
        if self._elapsed_seconds is not None and self._status in {"ok", "error"}:
            meta_bits.append(f"{self._elapsed_seconds:.2f}s")
        if self._exit_code is not None:
            meta_bits.append(f"exit={self._exit_code}")
        meta = " · ".join(meta_bits)
        tool_color = self._theme_variable("tool", "#C7ACE8")
        glyph_token = {
            "ok": "success",
            "approved": "success",
            "error": "error",
            "denied": "error",
            "pending": "warning",
        }.get(self._status, "tool")
        glyph_color = self._theme_variable(glyph_token, tool_color)
        meta_color = self._theme_variable("activity-meta", "#948A80")
        header = Text(f"{glyph} ", style=f"bold {glyph_color}")
        header.append(self.tool_name, style=f"bold {tool_color}")
        if meta:
            header.append(f"  ({meta})", style=meta_color)
        self._header.update(header)

    def _theme_variable(self, token: str, fallback: str) -> str:
        return theme_color(cast(App[object], self.app), token, fallback)  # type: ignore[reportUnknownMemberType]

    def _refresh_body(self) -> None:
        if self._args is None or self._compact:
            self._body.update("")
            self._body.display = False
            return
        self._body.display = True
        # edit_file: render a find -> replace diff instead of raw JSON args.
        # A diff communicates the change far more compactly than dumping both
        # strings, and gives the Claude-Code "see exactly what changed"
        # affordance. All other tools keep the existing JSON-args rendering.
        if self.tool_name == "edit_file":
            find = self._args.get("find")
            replace = self._args.get("replace")
            if isinstance(find, str) and isinstance(replace, str):
                self._body.update(self._render_edit_diff(find, replace))
                return
        rendered = json.dumps(self._args, ensure_ascii=False, indent=2)
        if not self._expanded and len(rendered) > _COLLAPSED_ARG_CHARS:
            rendered = rendered[:_COLLAPSED_ARG_CHARS].rstrip() + "\n… [E to expand]"
        self._body.update(rendered)

    def _diff_color(self, attr: str, fallback: str) -> str:
        """Resolve a theme color hex for diff styling.

        Returns the active theme's ``attr`` color (e.g. ``success``) so the
        diff adapts to lumen-dark/lumen-light, falling back to a Rich named
        color if the theme is somehow unavailable.
        """

        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        theme = app.current_theme
        color = getattr(theme, attr, None)
        return color if isinstance(color, str) and color else fallback

    def _render_edit_diff(self, find: str, replace: str) -> Text:
        """Render a compact line-level diff of edit_file's find -> replace.

        Diff generation and coloring are shared with the approval panel via
        :mod:`lumen.ui.diff_view`. Collapsed state shows a one-line change
        summary plus a preview of the first changed line; expanded shows the
        full unified diff so the user can inspect exactly what changed.
        """

        body_lines = unified_diff_lines(find, replace, context_lines=1)
        add_color = self._diff_color("success", "green")
        del_color = self._diff_color("error", "red")

        if not body_lines:
            return Text("~ no textual change", style="dim")

        added = sum(1 for line in body_lines if line.startswith("+"))
        removed = sum(1 for line in body_lines if line.startswith("-"))

        if not self._expanded:
            text = Text()
            text.append(f"~ +{added} -{removed}", style="dim")
            # Preview the first added line (what the code becomes); fall back to
            # the first removed line for pure deletions so the preview always
            # shows something informative.
            first_change = next((line for line in body_lines if line.startswith("+")), None) or next(
                (line for line in body_lines if line.startswith("-")), None
            )
            if first_change:
                preview = first_change[1:].rstrip()
                if len(preview) > 50:
                    preview = preview[:50].rstrip() + "…"
                text.append("  ")
                text.append(
                    preview,
                    style=add_color if first_change.startswith("+") else del_color,
                )
            text.append("  [E to expand]", style="dim")
            return text

        return style_diff_lines(body_lines, add_color=add_color, del_color=del_color)

    def _refresh_result(self) -> None:
        if self._result is None or self._compact:
            self._result_widget.update("")
            self._result_widget.display = False
            return
        self._result_widget.display = True
        if self._expanded:
            rendered = self._result
        else:
            rendered = self._preview if self._preview is not None else self._result
            if len(rendered) > _COLLAPSED_ARG_CHARS:
                rendered = rendered[:_COLLAPSED_ARG_CHARS].rstrip() + "\n… [E to expand]"
        self._result_widget.update(rendered)


__all__ = ["ToolCard"]
