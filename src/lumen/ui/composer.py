"""Self-contained state and Textual adapter for the TUI composer.

The Textual widget remains a thin event adapter; paste expansion, draft state,
and the kill ring live here so they can be tested without mounting an app.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast

from rich.cells import cell_len
from textual.app import App
from textual.binding import BindingType
from textual.message import Message
from textual.widgets import TextArea

from lumen.interactive_queue import QueueMode
from lumen.ui.autocomplete import CompletionDropdown


@dataclass(slots=True)
class ComposerState:
    """Durable editor state independent from Agent run state."""

    draft: str = ""
    kill_ring: str = ""
    _pastes: dict[str, str] = field(default_factory=lambda: {})
    _next_paste_id: int = 1

    def compact_paste(self, text: str) -> str:
        """Store a large paste and return its human-readable marker."""

        paste_id = self._next_paste_id
        self._next_paste_id += 1
        lines = text.count("\n") + 1
        byte_count = len(text.encode("utf-8"))
        marker = f"[pasted {lines} lines / {byte_count} bytes #{paste_id}]"
        self._pastes[marker] = text
        return marker

    def expand(self, text: str) -> str:
        """Expand every paste marker before submission to the runtime."""

        for marker, value in self._pastes.items():
            text = text.replace(marker, value)
        return text

    def clear_submitted(self) -> None:
        self.draft = ""


@dataclass(frozen=True, slots=True)
class HistoryResult:
    text: str
    browsing: bool


class ComposerHistory:
    """Prompt history with lossless draft restoration."""

    def __init__(self, *, max_entries: int = 50) -> None:
        self.max_entries = max_entries
        self.entries: list[str] = []
        self.index: int | None = None
        self.draft = ""

    def seed(self, entries: list[str]) -> None:
        self.entries = list(entries[-self.max_entries :])
        self.index = None
        self.draft = ""

    def record(self, text: str) -> None:
        if not text.strip() or (self.entries and self.entries[-1] == text):
            return
        self.entries.append(text)
        self.entries = self.entries[-self.max_entries :]
        self.index = None
        self.draft = ""

    def navigate(self, current_text: str, direction: int) -> HistoryResult | None:
        if not self.entries:
            return None
        if direction < 0:
            if self.index is None:
                self.draft = current_text
                self.index = len(self.entries) - 1
            else:
                self.index = max(0, self.index - 1)
        else:
            if self.index is None:
                return None
            self.index += 1
            if self.index >= len(self.entries):
                self.index = None
                restored = self.draft
                self.draft = ""
                return HistoryResult(restored, browsing=False)
        return HistoryResult(self.entries[self.index], browsing=True)


def is_large_paste(text: str) -> bool:
    """Use a marker when a paste would materially disrupt the composer."""

    return text.count("\n") >= 7 or len(text.encode("utf-8")) >= 4096


class PromptEditor(TextArea):
    """Multi-line prompt editor with @path and /command completion.

    The editor owns keyboard routing: when the completion dropdown is open,
    Up/Down/Tab/Esc go to it (the dropdown never takes focus so the user can
    keep typing). When the dropdown is closed and the cursor is on the first
    line, Up/Down navigate prompt history.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        # Key routing lives in ``on_key`` rather than bindings, because
        # TextArea's ``_on_key`` message handler eagerly inserts a newline on
        # Enter before any binding can run. We intercept Enter there to make
        # it submit, and Shift+Enter to insert a newline. When the completion
        # dropdown is open, Enter/Tab accept the highlighted suggestion
        # instead.
    ]

    class Submitted(Message):
        def __init__(
            self,
            text: str,
            mode: QueueMode = QueueMode.STEER,
            *,
            model_prompt: str | None = None,
        ) -> None:
            super().__init__()
            self.text = text
            self.model_prompt = model_prompt if model_prompt is not None else text
            self.mode = mode

    class DequeueRequested(Message):
        pass

    class CompletionRequested(Message):
        """Posted on text change so the app can refresh the dropdown.

        ``prefix`` is the trigger token at the cursor (``"@foo"``, ``"/"``,
        ``""`` when no completion is active).
        """

        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    class HistoryNavigation(Message):
        """Posted when the user navigates prompt history (Up/Down on row 0)."""

        def __init__(self, direction: int) -> None:
            super().__init__()
            # -1 = older (Up), +1 = newer (Down)
            self.direction = direction

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.state = ComposerState()
        # Injected by the app after mount.
        self._dropdown: CompletionDropdown | None = None
        # Guard: when a suggestion is accepted, _replace_token_at_cursor
        # mutates the editor text, which fires on_text_area_changed →
        # CompletionRequested. Without this guard, the dropdown would
        # immediately reopen because the replacement text (e.g.
        # ``@README.md ``) still starts with ``@``. We set this before the
        # replacement and clear it on the next change event.
        self._suppress_completion = False
        self._vim_enabled = False
        self._vim_insert = True

    def configure_vim(self, enabled: bool) -> None:
        """Enable a small, predictable Vim navigation layer for terminal users."""

        self._vim_enabled = enabled
        self._vim_insert = True
        self.set_class(enabled, "vim-enabled")

    def expanded_text(self) -> str:
        """Return model-facing text with compact paste markers expanded."""

        return self.state.expand(self.text)

    def restore_text(self, text: str) -> None:
        """Restore a draft without losing its cursor position."""

        self.text = text
        self.cursor_location = self.document.end

    def on_paste(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        pasted = str(getattr(event, "text", ""))
        if not is_large_paste(pasted):
            return
        self.insert(self.state.compact_paste(pasted))
        event.prevent_default()
        event.stop()

    def bind_dropdown(self, dropdown: CompletionDropdown) -> None:
        self._dropdown = dropdown

    @property
    def dropdown_open(self) -> bool:
        return self._dropdown is not None and self._dropdown.is_open

    # -- key routing -------------------------------------------------------

    async def on_key(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        """Route keys before TextArea's ``_on_key`` can consume them.

        TextArea's message handler eagerly inserts a newline on Enter, so we
        intercept Enter here and decide what to do based on state:
          - active trigger token → accept the highlighted suggestion
          - Shift+Enter          → insert a newline (multi-line prompt)
          - plain Enter          → submit the prompt
        Up/Down on row 0 walk prompt history. Everything else falls through to
        TextArea's default editing behaviour.

        **Synchronous trigger detection**: the old code checked
        ``self.dropdown_open`` (a derived, asynchronously-updated flag) to
        decide whether Enter means "accept completion" or "submit". When the
        user typed fast (``/q`` + Enter), the ``CompletionRequested`` message
        hadn't been processed yet, so ``dropdown_open`` was still False and
        Enter submitted a literal ``/q``. We now re-derive the trigger token
        *synchronously* in ``on_key``, so the decision is always fresh.
        """

        key = getattr(event, "key", "")

        if self._vim_enabled:
            if key == "escape" and self._vim_insert:
                self._vim_insert = False
                self.notify("Vim: NORMAL", timeout=1)
                event.prevent_default()
                event.stop()
                return
            if not self._vim_insert:
                if key in {"i", "a"}:
                    if key == "a":
                        self.action_cursor_right()
                    self._vim_insert = True
                    self.notify("Vim: INSERT", timeout=1)
                elif key == "h":
                    self.action_cursor_left()
                elif key == "j":
                    self.action_cursor_down()
                elif key == "k":
                    self.action_cursor_up()
                elif key == "l":
                    self.action_cursor_right()
                elif key == "x":
                    self.action_delete_right()
                else:
                    event.prevent_default()
                    event.stop()
                    return
                event.prevent_default()
                event.stop()
                return

        # --- completion dropdown takes over navigation when open ----------
        # We check the trigger token synchronously instead of relying on the
        # asynchronously-updated dropdown_open flag. This eliminates the race
        # where fast typing left the dropdown closed when Enter arrived.
        trigger = self._current_trigger_token()
        completion_active = bool(trigger) and self._dropdown is not None and self._dropdown.is_open

        if completion_active and self._dropdown is not None:
            if key == "up":
                self._dropdown.move_cursor(-1)
                event.prevent_default()
                event.stop()
                return
            if key == "down":
                self._dropdown.move_cursor(1)
                event.prevent_default()
                event.stop()
                return
            if key in ("tab", "enter"):
                # Accept the highlighted suggestion. Enter here does NOT
                # submit — it confirms the completion.
                self._dropdown.action_select(activate=key == "enter")
                event.prevent_default()
                event.stop()
                return
            if key == "escape":
                self._dropdown.action_dismiss()
                event.prevent_default()
                event.stop()
                return
            # Any other key falls through so the user keeps typing and the
            # dropdown refreshes via on_text_area_changed.
            return

        # --- Shift+Enter inserts a newline --------------------------------
        # Textual represents Shift+Enter as key=="shift+enter". We stop the
        # event so TextArea's _on_key doesn't double-handle it, and insert
        # the newline ourselves.
        if key == "shift+enter":
            self.insert("\n")
            event.prevent_default()
            event.stop()
            return

        if key == "alt+enter":
            self.action_submit(QueueMode.FOLLOW_UP)
            event.prevent_default()
            event.stop()
            return

        if key == "alt+up":
            self.post_message(self.DequeueRequested())
            event.prevent_default()
            event.stop()
            return

        if key == "ctrl+k":
            row, col = self.cursor_location
            line = self.document.get_line(row)
            self.state.kill_ring = line[col:] or "\n"
            if line[col:]:
                self.replace("", start=(row, col), end=(row, len(line)))
            elif row < self.document.line_count - 1:
                self.replace("", start=(row, col), end=(row + 1, 0))
            event.prevent_default()
            event.stop()
            return

        if key == "ctrl+y" and self.state.kill_ring:
            self.insert(self.state.kill_ring)
            event.prevent_default()
            event.stop()
            return

        if key == "ctrl+g":
            await self._open_external_editor()
            event.prevent_default()
            event.stop()
            return

        # --- plain Enter submits the prompt -------------------------------
        # This is the primary binding. We must stop the event before
        # TextArea._on_key turns it into a literal newline.
        if key == "enter":
            self.action_submit()
            event.prevent_default()
            event.stop()
            return

        # --- Up/Down at the start of the document → history ---------------
        if key in ("up", "down"):
            row, col = self.cursor_location
            if row == 0 and col == 0:
                direction = -1 if key == "up" else 1
                self.post_message(self.HistoryNavigation(direction))
                event.prevent_default()
                event.stop()
                return

        # --- Ctrl+Up / Ctrl+Down: always navigate history -----------------
        # Unlike bare Up/Down (which only trigger history at row 0, col 0),
        # these work regardless of cursor position — matching pi/tui's
        # Emacs-style modifier+arrow bindings for history navigation.
        if key in ("ctrl+up", "ctrl+down"):
            direction = -1 if key == "ctrl+up" else 1
            self.post_message(self.HistoryNavigation(direction))
            event.prevent_default()
            event.stop()
            return

    # -- text change → completion refresh ---------------------------------

    def on_text_area_changed(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        self._resize_to_content()
        # If a suggestion was just accepted, the text change from
        # _replace_token_at_cursor would re-trigger completion. Suppress it
        # for this one event.
        if self._suppress_completion:
            self._suppress_completion = False
            return
        # Re-derive the trigger token at the cursor and ask the app to refresh
        # the dropdown. The app decides whether to show file or slash options.
        prefix = self._current_trigger_token()
        self.post_message(self.CompletionRequested(prefix))

    def _resize_to_content(self) -> None:
        """Grow with wrapped visual rows, capped at 30% of the terminal."""

        available = max(1, self.size.width - 4)
        visual_rows = sum(
            max(1, (cell_len(line) + available - 1) // available)
            for line in self.text.split("\n")
        )
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        screen_height = app.size.height if self.is_attached else 24
        self.styles.height = min(max(3, visual_rows + 2), max(3, int(screen_height * 0.3)))

    def _current_trigger_token(self) -> str:
        """Return the @ / / token at the cursor, or ``""`` if none.

        Used to decide which completion source the dropdown should show. We
        look at the current line from its start (or last whitespace) to the
        cursor and check whether it begins with ``@`` or ``/``.

        Both ``@`` and ``/`` trigger at any token boundary (after whitespace
        or at line start) — not just at column 0. This makes ``/exit`` work
        mid-line (e.g. after indentation) and keeps ``@`` and ``/`` behaviour
        symmetric.
        """

        row, col = self.cursor_location
        line = self.document.get_line(row) if col > 0 else ""
        before = line[:col]
        # Find the last whitespace; the token starts after it.
        idx = max(before.rfind(" "), before.rfind("\t"))
        token = before[idx + 1 :] if idx >= 0 else before
        if token.startswith("@") or token.startswith("/"):
            return token
        return ""

    def action_submit(self, mode: QueueMode = QueueMode.STEER) -> None:
        display_text = self.text.strip()
        model_prompt = self.expanded_text().strip()
        if display_text:
            self.post_message(self.Submitted(display_text, mode, model_prompt=model_prompt))
            self.clear()
            self.state.clear_submitted()
            # Dismiss any open dropdown on submit.
            if self._dropdown is not None:
                self._dropdown.hide()

    async def _open_external_editor(self) -> None:
        updated = await edit_text_external(self.expanded_text())
        if updated is None:
            self.notify("Set $VISUAL or $EDITOR to use Ctrl+G", severity="warning")
            return
        original_cursor = self.cursor_location
        self.text = updated
        end_row, end_col = self.document.end
        self.cursor_location = (min(original_cursor[0], end_row), min(original_cursor[1], end_col))


async def edit_text_external(value: str, *, suffix: str = ".md") -> str | None:
    """Round-trip text through the configured editor without invoking a shell."""

    command = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not command:
        return None

    def edit() -> str:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, encoding="utf-8", delete=False
            ) as handle:
                handle.write(value)
                path = Path(handle.name)
            completed = subprocess.run([*shlex.split(command), str(path)], check=False)
            return value if completed.returncode != 0 else path.read_text(encoding="utf-8")
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    return await asyncio.to_thread(edit)


__all__ = [
    "ComposerHistory",
    "ComposerState",
    "HistoryResult",
    "PromptEditor",
    "edit_text_external",
    "is_large_paste",
]
