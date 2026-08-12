"""Completion dropdown wiring, extracted verbatim from ``app.py``.

``CompletionControllerMixin`` owns the ``@``/``/`` completion flow: editor
messages, debounced file search, and slash-command suggestions. ``LumenApp``
is only imported under ``TYPE_CHECKING`` to avoid a circular import.
"""

# Cooperative Textual mixin; see approval_controller.py for why these two
# diagnostics are disabled locally rather than weakening project-wide strictness.
# pyright: reportGeneralTypeIssues=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual import on
from textual.message_pump import MessagePump

from lumen.ui.autocomplete import CompletionDropdown, CompletionSuggestion
from lumen.ui.composer import PromptEditor
from lumen.ui.file_search import FileSearchHandle, build_suggestion
from lumen.ui.slash_commands import iter_visible

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class CompletionControllerMixin(MessagePump):
    """Route editor keystrokes into the floating completion dropdown.

    Inherits ``MessagePump`` so Textual's metaclass registers the ``@on``
    handlers below into this class's ``_decorated_handlers``; dispatch walks
    ``LumenApp``'s MRO and finds them here.
    """

    @on(PromptEditor.CompletionRequested)
    async def _on_completion_requested(self: LumenApp, event: PromptEditor.CompletionRequested) -> None:
        await self._refresh_completions(event.prefix)

    @on(PromptEditor.HistoryNavigation)
    def _on_history_navigation(self: LumenApp, event: PromptEditor.HistoryNavigation) -> None:
        editor = self.query_one("#prompt", PromptEditor)
        result = self._composer_history.navigate(editor.text, event.direction)
        if result is None:
            return
        editor.text = result.text
        # Keep the cursor at the start so repeated Up/Down presses keep
        # walking the stack. The user can press Right / End / click to edit
        # the recalled prompt; once they do, history navigation stops because
        # the cursor is no longer at (0, 0).
        editor.cursor_location = (0, 0) if result.browsing else editor.document.end

    @on(CompletionDropdown.SuggestionSelected)
    def _on_suggestion_selected(self: LumenApp, event: CompletionDropdown.SuggestionSelected) -> None:
        editor = self.query_one("#prompt", PromptEditor)
        # Set the suppress guard so the text mutation from replace() doesn't
        # immediately re-open the dropdown (the inserted text like
        # ``@README.md `` still starts with ``@``).
        editor._suppress_completion = True  # type: ignore[reportPrivateUsage]
        # Replace the trigger token at the cursor with the chosen suggestion's
        # ``insert`` text. The token is whatever the editor reported as the
        # current trigger prefix (e.g. "@src/" or "/mode").
        self._replace_token_at_cursor(editor, event.prefix, event.suggestion.insert)
        editor.focus()
        dropdown = self.query_one(CompletionDropdown)
        dropdown.hide()

    @on(CompletionDropdown.Dismissed)
    def _on_completion_dismissed(self: LumenApp, event: CompletionDropdown.Dismissed) -> None:
        self.query_one(CompletionDropdown).hide()
        self.query_one("#prompt", PromptEditor).focus()

    @staticmethod
    def _replace_token_at_cursor(editor: PromptEditor, token: str, replacement: str) -> None:
        """Replace the trigger ``token`` at the cursor with ``replacement``.

        ``token`` is the prefix the editor extracted (e.g. ``"@src"``); we
        overwrite the slice [col-len(token), col) on the current row with
        ``replacement``. If the editor's text drifted (user kept typing after
        the dropdown opened), we fall back to inserting at the cursor.
        """

        if not token:
            return
        row, col = editor.cursor_location
        line = editor.document.get_line(row)
        start = col - len(token)
        if start < 0 or line[start:col] != token:
            editor.insert(replacement)
            return
        # ``replace`` takes (start, end) locations as (row, col) tuples.
        editor.replace(replacement, start=(row, start), end=(row, col))

    async def _refresh_completions(self: LumenApp, prefix: str) -> None:
        """Populate the dropdown based on the trigger prefix.

        ``prefix`` is the editor's current trigger token:
        - ``"@…"`` → tree-wide file search under the workspace root
        - ``"/"`` (and only at the very start of the input) → slash commands
        - ``""`` → no completion; hide the dropdown

        After populating file suggestions we reposition the dropdown above the
        prompt editor so it never overlaps the input box.
        """

        self._completion_generation += 1
        generation = self._completion_generation
        if self._completion_search_handle is not None:
            self._completion_search_handle.cancel()
            self._completion_search_handle = None
        dropdown = self.query_one(CompletionDropdown)
        if not prefix:
            dropdown.hide()
            return

        if prefix.startswith("@"):
            # Tree-wide search: ``@app`` finds ``src/lumen/ui/app.py``
            # without making the user descend directory by directory.
            # search_files shells out to fd (or falls back to os.walk) which
            # is blocking I/O — run it in a thread so the UI stays responsive.
            # Without this the TUI freezes for up to 2s per ``@`` keystroke.
            await asyncio.sleep(0.075)
            if generation != self._completion_generation:
                return
            # Resolve the search function through the app module at call time:
            # tests monkeypatch ``lumen.ui.app.search_files`` to substitute a
            # controlled filesystem search.
            from lumen.ui import app as app_module

            handle = FileSearchHandle(
                prefix,
                self.resources.registry.workspace,
                search=app_module.search_files,
            )
            self._completion_search_handle = handle
            try:
                hits = await asyncio.to_thread(handle.run)
            finally:
                if self._completion_search_handle is handle:
                    self._completion_search_handle = None
            if generation != self._completion_generation:
                return
            suggestions = [build_suggestion(h, prefix) for h in hits]
            if not suggestions:
                # Show an explicit "no matches" state instead of silently
                # hiding the dropdown — the user needs feedback that the
                # search ran and found nothing.
                dropdown.set_suggestions(
                    [CompletionSuggestion(label="(no files found)", insert=prefix)],
                    prefix=prefix,
                )
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
                return
            dropdown.set_suggestions(suggestions, prefix=prefix)
            if suggestions:
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
            return

        if prefix.startswith("/"):
            suggestions = self._slash_command_suggestions(prefix)
            if not suggestions:
                dropdown.set_suggestions(
                    [CompletionSuggestion(label="(no matching commands)", insert=prefix)],
                    prefix=prefix,
                )
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
                return
            dropdown.set_suggestions(suggestions, prefix=prefix)
            if suggestions:
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
            return

        dropdown.hide()

    def _slash_command_suggestions(self: LumenApp, prefix: str) -> list[CompletionSuggestion]:
        """List the built-in slash commands matching ``prefix``.

        Static rows come from the slash-command registry (hidden commands and
        aliases excluded); dynamic ``/skill:<name>`` entries are appended for
        every discovered skill, so the user can discover and invoke skills
        from the completion dropdown just like builtin commands.
        """

        # Commands whose completion description embeds live state — the
        # registry holds the static text; these overrides keep the current
        # value visible like before.
        dynamic_descriptions = {
            "model": f"Switch model · current: {self._model_display()}",
            "mode": f"Approval policy · current: {self._approval_mode}",
            "tools": f"List visible tools · {len(self.resources.tool_metadata)} loaded",
            "hooks": f"List configured hooks · {len(self.resources.hooks.hooks)} loaded",
            "skills": f"List available skills · {len(self.resources.skills)} loaded",
        }
        commands: list[tuple[str, str]] = []
        for entry in iter_visible():
            if entry.name == "skill:":
                continue  # dynamic rows are generated below
            description = dynamic_descriptions.get(entry.name, entry.description)
            commands.append((f"/{entry.name}", description))
            for suffix, suffix_description in entry.completion_rows:
                commands.append((f"/{entry.name} {suffix}", suffix_description))
        # Dynamic /skill:<name> entries — one per discovered skill, with the
        # skill description truncated for dropdown readability.
        for skill in self.resources.skills:
            commands.append((f"/skill:{skill.name}", skill.description[:60]))
        frag = prefix  # prefix already starts with "/"
        return [
            CompletionSuggestion(label=cmd, insert=cmd + " ", description=desc)
            for cmd, desc in commands
            if cmd.startswith(frag)
        ]
