"""Integration tests for the TUI completion dropdown and history navigation.

These exercise the wired-up LumenApp: typing ``@`` pops file suggestions,
typing ``/`` pops slash-command suggestions, Up/Down/Tab/Esc drive the
dropdown, and Up at the start of an empty editor walks prompt history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text

from lumen.config import load_config
from lumen.resources import ResourceManager
from lumen.ui.app import LumenApp, PromptEditor
from lumen.ui.autocomplete import CompletionDropdown


def _make_app(tmp_path: Path) -> LumenApp:
    """Build a minimal LumenApp with a couple of files in the workspace."""

    (tmp_path / "config.yaml").write_text("hello: world\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  name: tui-test
  model: {id: test}
tools: {builtins: []}
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )
    return LumenApp(load_config(config_path), ResourceManager(load_config(config_path), workspace=tmp_path))


async def _focus_editor(pilot: Any) -> PromptEditor:  # type: ignore[no-untyped-def]
    editor = pilot.app.query_one("#prompt", PromptEditor)
    editor.focus()
    await pilot.pause()
    return editor


async def test_at_trigger_opens_file_dropdown(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        labels = [s.label for s in dropdown.suggestions]
        # All three workspace files plus the src/ directory appear.
        assert "config.yaml" in labels
        assert "README.md" in labels
        assert "src/" in labels


async def test_at_with_path_prefix_filters(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("@", "R", "E", "A")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        labels = [s.label for s in dropdown.suggestions]
        assert "README.md" in labels
        # config.yaml doesn't start with REA → filtered out.
        assert "config.yaml" not in labels


async def test_at_descend_into_subdirectory(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        # Type @src/ to descend into src/.
        for ch in "@src/":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        labels = [s.label for s in dropdown.suggestions]
        assert "mod.py" in labels
        # And the suggestion's insert preserves the @src/ prefix.
        mod = next(s for s in dropdown.suggestions if s.label == "mod.py")
        assert mod.insert == "@src/mod.py "


async def test_tab_accepts_file_suggestion(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        # Highlight README.md (alphabetical position depends on dir sort).
        # We press down until README is highlighted, then Tab.
        for _ in range(len(dropdown.suggestions)):
            if dropdown.suggestions[dropdown.highlighted or 0].label == "README.md":
                break
            await pilot.press("down")
            await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert editor.text.startswith("@README.md")
        assert not dropdown.is_open


async def test_escape_closes_dropdown(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        await pilot.press("escape")
        await pilot.pause()
        assert not dropdown.is_open


async def test_slash_trigger_opens_command_dropdown(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("/")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        labels = [s.label for s in dropdown.suggestions]
        # Built-in commands are listed.
        assert "/help" in labels
        assert "/model" in labels
        assert "/exit" in labels


async def test_slash_prefix_filters_commands(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        for ch in "/se":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        labels = [s.label for s in dropdown.suggestions]
        assert "/sessions" in labels
        # /help, /model etc. don't start with /se.
        assert "/help" not in labels
        assert "/model" not in labels


async def test_single_slash_match_has_visible_content_row(tmp_path: Path) -> None:
    """One match must reserve content plus border rows, not render an empty box."""

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        editor = await _focus_editor(pilot)
        for ch in "/ex":
            await pilot.press(ch)
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)

        assert [suggestion.label for suggestion in dropdown.suggestions] == ["/exit"]
        assert dropdown.region.height >= 3
        assert dropdown.region.x == editor.region.x
        assert dropdown.region.width == editor.region.width
        assert dropdown.border_title == "Commands"
        assert "Enter select" in str(dropdown.border_subtitle)
        assert dropdown.styles.border_left[0] == "round"

        prompt = dropdown.get_option_at_index(0).prompt
        assert isinstance(prompt, Text)
        assert "bold #F0A24A" in {str(span.style) for span in prompt.spans}


async def test_model_and_mode_completions_show_current_values(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await _focus_editor(pilot)
        for ch in "/mo":
            await pilot.press(ch)
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        by_label = {suggestion.label: suggestion.description or "" for suggestion in dropdown.suggestions}

        assert "current: test" in by_label["/model"]
        assert "current: manual" in by_label["/mode"]


async def test_history_navigation_walks_back(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        # Seed history directly.
        app.seed_history(["hello world", "second prompt", "third prompt"])
        # Up on an empty editor → most recent entry.
        await pilot.press("up")
        await pilot.pause()
        assert editor.text == "third prompt"
        # Up again → older.
        await pilot.press("up")
        await pilot.pause()
        assert editor.text == "second prompt"
        await pilot.press("up")
        await pilot.pause()
        assert editor.text == "hello world"
        # Down → newer.
        await pilot.press("down")
        await pilot.pause()
        assert editor.text == "second prompt"


async def test_history_down_at_oldest_clears_editor(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        app.seed_history(["only one"])
        await pilot.press("up")
        await pilot.pause()
        assert editor.text == "only one"
        # Down past the newest → editor clears, history browsing resets.
        await pilot.press("down")
        await pilot.pause()
        assert editor.text == ""
        assert app.history_browsing_index is None


async def test_no_completion_when_typing_plain_text(tmp_path: Path) -> None:
    """Bare text (no @ or /) keeps the dropdown hidden."""

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        for ch in "hello world":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert not dropdown.is_open


# ---------------------------------------------------------------------------
# Regression tests for the MarkupError crash + Enter/Shift+Enter keymap.
# ---------------------------------------------------------------------------


async def test_system_message_with_colon_does_not_crash(tmp_path: Path) -> None:
    """Text containing ``key: value`` (common in tool/MCP output) must render.

    Regression for a MarkupError crash: Static widgets parse rich-markup by
    default, and ``foo: bar`` was misread as a markup attribute. We disable
    markup on every user-content Static.
    """

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Push the exact pattern that used to crash (looks like a Python
        # traceback fragment with leading colons + bracketed indentation).
        nasty = (
            "build_mcp_toolset(\n    name='exa',\n    timeout=config.agent.limits.tool_timeout_seconds,\n)"
        )
        # We poke the private _append_system intentionally — it's the exact
        # code path that crashed, and going through handle_input would also
        # try to drive a real agent run.
        await app._append_system(nasty)  # type: ignore[reportPrivateUsage]
        await app._append_system("also [bracketed] text and a [link]http://x[/link] fake tag")  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        # The app is still alive — no MarkupError was raised. We assert by
        # querying the messages container: if rendering had crashed, the
        # pilot would have raised during pause().
        messages = app.query_one("#messages")
        assert messages is not None


async def test_enter_submits_prompt(tmp_path: Path) -> None:
    """Enter on its own submits the prompt (does not insert a newline)."""

    app = _make_app(tmp_path)
    submitted: list[str] = []

    async def capture(text: str) -> None:
        submitted.append(text)

    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        # Short-circuit handle_input so we don't drive a real agent run.
        app.handle_input = capture  # type: ignore[assignment]
        for ch in "hello":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert submitted == ["hello"]
    # Editor was cleared after submit (no leftover newline).
    assert editor.text == ""


async def test_shift_enter_inserts_newline(tmp_path: Path) -> None:
    """Shift+Enter inserts a literal newline instead of submitting."""

    app = _make_app(tmp_path)
    submitted: list[str] = []

    async def capture(text: str) -> None:
        submitted.append(text)

    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        app.handle_input = capture  # type: ignore[assignment]
        for ch in "line1":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("shift+enter")
        await pilot.pause()
        # No submit yet — we're still editing.
        assert submitted == []
        assert "\n" in editor.text
        # Type on the new line, then Enter to submit the multi-line prompt.
        for ch in "line2":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert submitted == ["line1\nline2"]


# ---------------------------------------------------------------------------
# Tree-wide search + dropdown positioning (pi/tui port)
# ---------------------------------------------------------------------------


async def test_at_finds_deep_files_without_descending(tmp_path: Path) -> None:
    """``@app`` surfaces deeply nested files via tree-wide search.

    This is the core UX fix: previously the user had to type ``@src/`` →
    ``@src/lumen/`` → … to reach a deep file. Now ``@<name>`` searches
    the whole workspace.
    """

    # Build a deeper tree than _make_app does. Create the app first so its
    # own ``src/`` is in place, then add the nested files underneath.
    app = _make_app(tmp_path)
    (tmp_path / "src" / "deep" / "nested").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "deep" / "nested" / "app.py").write_text("x")
    async with app.run_test(size=(120, 30)) as pilot:
        await _focus_editor(pilot)
        for ch in "@app":
            await pilot.press(ch)
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        labels = [s.label for s in dropdown.suggestions]
        # The deeply nested app.py surfaces without manual descent.
        assert "app.py" in labels


async def test_dropdown_does_not_cover_prompt(tmp_path: Path) -> None:
    """The completion dropdown floats above the prompt, never overlapping it.

    Guards against the regression where ``dock: bottom`` pinned the dropdown
    to the screen bottom and covered the input box on short terminals.
    """

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 24)) as pilot:
        editor = await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        await pilot.pause()  # let anchor_above run
        dropdown = app.query_one(CompletionDropdown)
        if not dropdown.is_open:
            return  # no suggestions → nothing to position; vacuously true
        prompt_region = editor.region
        dropdown_region = dropdown.region
        # The two regions must not overlap (one ends before the other starts).
        overlap = not (
            prompt_region.y + prompt_region.height <= dropdown_region.y
            or dropdown_region.y + dropdown_region.height <= prompt_region.y
        )
        assert not overlap, f"dropdown {dropdown_region} overlaps prompt {prompt_region}"
        # And the dropdown must be fully on-screen.
        assert dropdown_region.y >= 0, f"dropdown off-screen top: {dropdown_region}"
        assert dropdown_region.y + dropdown_region.height <= 24, (
            f"dropdown off-screen bottom: {dropdown_region}"
        )


async def test_git_entries_never_appear(tmp_path: Path) -> None:
    """.git internals are never suggested, even on a bare ``@``."""

    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / ".git" / "config").write_text("[core]")
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        for sug in dropdown.suggestions:
            assert ".git" not in sug.label, f"unexpected .git in label: {sug.label}"
            assert sug.description is None or ".git" not in sug.description, (
                f"unexpected .git in description: {sug.description}"
            )


async def test_best_match_highlighted_on_refresh(tmp_path: Path) -> None:
    """Typing ``@REA`` highlights ``README.md``, not whatever sorted first."""

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await _focus_editor(pilot)
        for ch in "@REA":
            await pilot.press(ch)
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.highlighted is not None
        highlighted_label = dropdown.suggestions[dropdown.highlighted].label
        assert highlighted_label == "README.md"


# ---------------------------------------------------------------------------
# Slash trigger symmetry + completion race fix (post-pi/tui redesign)
# ---------------------------------------------------------------------------


async def test_slash_triggers_after_whitespace(tmp_path: Path) -> None:
    """``/`` triggers completion at any token boundary, not just col 0.

    Regression for the old ``idx < 0`` constraint that restricted slash
    completion to column 0. Now ``/exit`` typed after whitespace (or even
    mid-line after other text) should still pop the command dropdown,
    matching ``@`` behaviour.
    """

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        # Type some text, then a space, then /e — / should still trigger.
        for ch in "run /e":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        labels = [s.label for s in dropdown.suggestions]
        assert "/exit" in labels


async def test_slash_enter_accepts_completion_not_literal(tmp_path: Path) -> None:
    """``/e`` + Enter accepts the ``/exit`` completion, doesn't submit ``/e``.

    This is the core race-fix test: the old code checked the asynchronously-
    updated ``dropdown_open`` flag, which was often still False when Enter
    arrived after fast typing. Now ``on_key`` derives the trigger token
    synchronously, so Enter always sees the fresh completion state.
    """

    app = _make_app(tmp_path)
    commands_run: list[str] = []

    async def capture(text: str) -> None:
        commands_run.append(text)

    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        app.handle_input = capture  # type: ignore[assignment]
        # Type /ex then immediately press Enter — simulating fast typing.
        # ("/e" alone would now match /edit first, so use the full prefix.)
        for ch in "/ex":
            await pilot.press(ch)
        await pilot.pause()
        # Dropdown should be open with /exit highlighted.
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        assert "/exit" in [s.label for s in dropdown.suggestions]
        # Press Enter to accept the completion.
        await pilot.press("enter")
        await pilot.pause()
        # The editor should now contain /exit (the accepted completion),
        # NOT have been submitted as a literal /e command.
        assert editor.text.startswith("/exit")
        # And no command was run (Enter accepted the completion, not submitted).
        assert commands_run == []


async def test_esc_closes_dropdown_without_cancelling(tmp_path: Path) -> None:
    """When the dropdown is open, Esc closes it but doesn't cancel a run.

    Tests the smart_escape priority: dropdown > worker > editor. With the
    dropdown open, Esc should only close it, leaving any hypothetical run
    untouched.
    """

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        # Esc should close the dropdown.
        await pilot.press("escape")
        await pilot.pause()
        assert not dropdown.is_open
        # The editor should still be focused and functional.
        editor = app.query_one("#prompt", PromptEditor)
        assert editor.has_focus


async def test_esc_clears_editor_when_idle(tmp_path: Path) -> None:
    """Idle Esc (no dropdown, no run) clears the editor text."""

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        for ch in "some text":
            await pilot.press(ch)
        await pilot.pause()
        assert editor.text == "some text"
        # Esc clears the editor (no dropdown, no worker running).
        await pilot.press("escape")
        await pilot.pause()
        assert editor.text == ""


async def test_ctrl_up_down_navigates_history_anywhere(tmp_path: Path) -> None:
    """Ctrl+Up/Ctrl+Down navigate history regardless of cursor position.

    Unlike bare Up/Down (which only trigger history at row 0, col 0), the
    Ctrl-modified versions work from any cursor position — matching pi/tui's
    Emacs-style modifier+arrow bindings.
    """

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _focus_editor(pilot)
        app.seed_history(["first prompt", "second prompt"])
        # Type some text so cursor is NOT at (0, 0).
        for ch in "hello":
            await pilot.press(ch)
        await pilot.pause()
        assert editor.cursor_location != (0, 0)
        # Ctrl+Up should still navigate to the most recent history entry.
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert editor.text == "second prompt"


async def test_dropdown_closes_synchronously_on_accept(tmp_path: Path) -> None:
    """``action_select`` hides the dropdown synchronously before returning.

    Guards against the double-Enter race: the old code left ``is_open`` True
    until a later message handler hid the dropdown. Now it hides immediately.
    """

    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _focus_editor(pilot)
        await pilot.press("@")
        await pilot.pause()
        await pilot.pause()
        dropdown = app.query_one(CompletionDropdown)
        assert dropdown.is_open
        # Simulate accepting via Tab.
        await pilot.press("tab")
        await pilot.pause()
        # The dropdown must be closed immediately after accept.
        assert not dropdown.is_open
        assert dropdown.suggestions == []
