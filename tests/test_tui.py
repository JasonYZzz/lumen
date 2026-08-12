from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from lumen.events import (
    ClarificationRequested,
    CommentaryDelta,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunCancelled,
    RunCompleted,
    RunStarted,
    RunWaitingForUser,
    TextDelta,
    TextRetracted,
    ToolApprovalPending,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.ui.activity_indicator import RunActivityIndicator
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.autocomplete import CompletionDropdown
from lumen.ui.file_search import FileHit
from lumen.ui.plan_panel import PlanPanel, render_plan_summary
from lumen.ui.streaming_markdown import AssistantMarkdown
from lumen.ui.tool_card import ToolCard

# ---------------------------------------------------------------------------
# PlanPanel tests
# ---------------------------------------------------------------------------


class PlanHost(App[None]):
    """Tiny host app that mounts a single PlanPanel."""

    def __init__(self, plan: PlanState | None = None) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        panel = PlanPanel()
        if self.plan is not None:
            # Defer the first update until after mount so children exist.
            self.call_after_refresh(lambda: panel.update_plan(self.plan))  # type: ignore[func-returns-value]
        yield panel


async def test_plan_panel_marks_active_and_completed_steps() -> None:
    plan = PlanState(
        steps=[
            PlanStep(id="one", title="Inspect", status=StepStatus.COMPLETED),
            PlanStep(id="two", title="Test", status=StepStatus.IN_PROGRESS),
        ]
    )
    app = PlanHost(plan)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(PlanPanel)
        summary = _panel_summary(panel)
    assert "✔ Inspect" in summary
    assert "▣ Test" in summary


async def test_plan_panel_updates_on_event() -> None:
    app = PlanHost()
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(PlanPanel)
        panel.update_plan(PlanState(steps=[PlanStep(id="x", title="A", status=StepStatus.PENDING)]))
        await pilot.pause()
        summary = _panel_summary(panel)
    assert "☐ A" in summary


async def test_plan_panel_collapse_api_toggles_class() -> None:
    plan = PlanState(steps=[PlanStep(id="one", title="Inspect", status=StepStatus.PENDING)])
    app = PlanHost(plan)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.pause()
        panel = app.query_one(PlanPanel)
        # The host doesn't wire resize events; assert the public API instead.
        panel.collapse(True)
        await pilot.pause()
        assert panel.has_class("plan-collapsed")
        assert "Tasks 0/1 · Inspect" in _panel_summary(panel)
        assert len(panel.query(".plan-step")) == 0
        panel.collapse(False)
        await pilot.pause()
        assert not panel.has_class("plan-collapsed")
        assert "☐ Inspect" in _panel_summary(panel)


def test_render_plan_summary_handles_blocked_state() -> None:
    plan = PlanState(
        steps=[
            PlanStep(id="b", title="Blocker", status=StepStatus.BLOCKED, note="missing input"),
        ]
    )
    summary = render_plan_summary(plan)
    assert "! Blocker — missing input" in summary


# ---------------------------------------------------------------------------
# ToolCard tests
# ---------------------------------------------------------------------------


class ToolCardHost(App[None]):
    """Mounts a single ToolCard for lifecycle testing."""

    def __init__(self, card: ToolCard) -> None:
        super().__init__()
        self.card = card
        self.decisions: list[tuple[str, bool]] = []
        self._decision_event = asyncio.Event()

    def compose(self) -> ComposeResult:
        yield Vertical(self.card)

    async def next_decision(self) -> tuple[str, bool]:
        await self._decision_event.wait()
        return self.decisions[0]

    def on_tool_card_decision(self, event: ToolCard.Decision) -> None:
        self.decisions.append((event.call_id, event.approved))
        self._decision_event.set()


async def test_tool_card_renders_running_then_finished() -> None:
    card = ToolCard("call-1", "read_file")
    app = ToolCardHost(card)
    async with app.run_test() as pilot:
        card.start(args={"path": "x"}, origin="builtin", risk="read")
        await pilot.pause()
        assert "●" in _header_text(card)
        assert "Reading" in _header_text(card)
        assert "read_file" not in _header_text(card)
        header = card.query_one(".tool-header", Static).content
        assert isinstance(header, Text)
        assert "bold #C7ACE8" in {str(span.style) for span in header.spans}
        card.update_result(result="ok", is_error=False, elapsed_seconds=0.5)
        await pilot.pause()
        assert "✓" in _header_text(card)
        assert "Read" in _header_text(card)


async def test_tool_card_uses_preview_until_expanded() -> None:
    card = ToolCard("call-preview", "read_file")
    app = ToolCardHost(card)
    long_argument = "a" * 1_500
    long_result = "full " * 400
    async with app.run_test() as pilot:
        card.start(args={"query": long_argument}, origin="builtin", risk="read")
        card.update_result(
            result=long_result,
            preview="short preview",
            is_error=False,
        )
        await pilot.pause()
        assert card.expanded is False
        assert card.displayed_result == "short preview"
        assert len(card.displayed_args) < len(long_argument)

        card.focus()
        await pilot.press("e")
        await pilot.pause()
        assert card.expanded is True
        assert card.displayed_result == long_result
        assert long_argument in card.displayed_args


async def test_tool_card_renders_edit_file_diff() -> None:
    """edit_file cards show a find->replace diff instead of raw JSON args."""

    card = ToolCard("call-edit", "edit_file")
    app = ToolCardHost(card)
    find = "old line\nsecond line"
    replace = "new line\nsecond line"
    async with app.run_test() as pilot:
        card.start(
            args={"path": "x.py", "find": find, "replace": replace},
            origin="builtin",
            risk="write",
        )
        await pilot.pause()
        # Collapsed: one-line summary with +1/-1 counts and a preview of the
        # first changed line - not a raw JSON dump of both strings.
        body = card.displayed_args
        assert "+1" in body and "-1" in body
        assert "new line" in body
        assert "[E to expand]" in body
        assert '"find"' not in body

        # Finish the card so the 'e' expand key is accepted (gated on ok/error).
        card.update_result(result="edited", is_error=False, elapsed_seconds=0.1)
        await pilot.pause()
        card.focus()
        await pilot.press("e")
        await pilot.pause()
        assert card.expanded is True
        expanded = card.displayed_args
        # Expanded: full unified diff with removed/added lines and context.
        assert "-old line" in expanded
        assert "+new line" in expanded
        assert "second line" in expanded


async def test_tool_card_edit_file_diff_no_change() -> None:
    """edit_file with identical find/replace reports no textual change."""

    card = ToolCard("call-noop", "edit_file")
    app = ToolCardHost(card)
    async with app.run_test() as pilot:
        card.start(
            args={"path": "x.py", "find": "same", "replace": "same"},
            origin="builtin",
            risk="write",
        )
        await pilot.pause()
        assert "no textual change" in card.displayed_args


async def test_tool_card_non_edit_tools_keep_json_args() -> None:
    """write_file and other tools are unaffected - still render JSON args."""

    card = ToolCard("call-write", "write_file")
    app = ToolCardHost(card)
    async with app.run_test() as pilot:
        card.start(
            args={"path": "out.md", "content": "hello"},
            origin="builtin",
            risk="write",
        )
        await pilot.pause()
        body = card.displayed_args
        assert '"path"' in body  # JSON args rendering preserved
        assert "[E to expand]" not in body  # short enough not to truncate


async def test_tool_card_resolves_inline_denial() -> None:
    card = ToolCard("call-2", "write_file")
    app = ToolCardHost(card)
    request = ToolApprovalPending(
        call_id="call-2", name="write_file", args={"path": "x"}, origin="builtin", risk="write"
    )
    async with app.run_test() as pilot:
        card.set_approval_pending(request)
        await pilot.pause()
        # Selector is focused on mount. Move down to highlight Deny, Enter.
        await pilot.press("down", "enter")
        await pilot.pause()
        decision = await app.next_decision()
    assert decision == ("call-2", False)


async def test_tool_card_resolves_inline_allow() -> None:
    card = ToolCard("call-3", "run_command")
    app = ToolCardHost(card)
    request = ToolApprovalPending(
        call_id="call-3", name="run_command", args={"argv": ["ls"]}, origin="builtin", risk="execute"
    )
    async with app.run_test() as pilot:
        card.set_approval_pending(request)
        await pilot.pause()
        await pilot.press("up", "enter")
        await pilot.pause()
        decision = await app.next_decision()
    assert decision == ("call-3", True)


async def test_tool_card_requires_explicit_selection_before_enter() -> None:
    card = ToolCard("call-explicit", "write_file")
    app = ToolCardHost(card)
    request = ToolApprovalPending(
        call_id="call-explicit", name="write_file", args={}, origin="builtin", risk="write"
    )
    async with app.run_test() as pilot:
        card.set_approval_pending(request)
        await pilot.pause()
        await pilot.press("enter", "tab", "y", "n")
        await pilot.pause()
        assert app.decisions == []
        await pilot.press("down", "enter")
        await pilot.pause()
    assert app.decisions == [("call-explicit", False)]


async def test_tool_card_ignores_keys_after_first_decision() -> None:
    card = ToolCard("call-4", "write_file")
    app = ToolCardHost(card)
    request = ToolApprovalPending(
        call_id="call-4", name="write_file", args={}, origin="builtin", risk="write"
    )
    async with app.run_test() as pilot:
        card.set_approval_pending(request)
        await pilot.pause()
        await pilot.press("up", "enter")
        await pilot.pause()
        # Pressing keys again must not produce a second decision — the card is
        # already resolved.
        await pilot.press("down", "enter")
        await pilot.pause()
    assert app.decisions == [("call-4", True)]


def _panel_summary(panel: PlanPanel) -> str:
    """Collect the rendered text from a PlanPanel's child Static widgets."""

    parts: list[str] = []
    for child in panel.children:
        content = getattr(child, "content", None)
        if isinstance(content, str):
            parts.append(content)
        else:
            renderable = getattr(child, "renderable", None)
            if isinstance(renderable, str):
                parts.append(renderable)
    return "\n".join(parts)


def _header_text(card: ToolCard) -> str:
    return str(getattr(card._header, "content", ""))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LumenApp integration tests
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    from lumen.config import load_config
    from lumen.resources import ResourceManager
    from lumen.ui.app import LumenApp

    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  name: tui-test
  model:
    id: test
tools:
  builtins: []
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


def _make_multi_model_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    from lumen.config import load_config
    from lumen.resources import ResourceManager
    from lumen.ui.app import LumenApp

    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  name: tui-test
  default_model: alpha
  models:
    alpha: {id: test, api_key: ka}
    beta: {id: test, api_key: kb}
tools: {builtins: []}
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


async def test_help_command_renders_command_list(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    async with app.run_test() as pilot:
        await app.handle_input("/help")
        await pilot.pause()
        text = "\n".join(str(widget.content) for widget in app.query("#messages Static").results(Static))

    assert "/resume" in text
    assert "/tools" in text
    assert "/model" in text
    assert "/clear" in text


async def test_clear_resets_only_visible_timeline(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    async with app.run_test() as pilot:
        await app.handle_input("/help")
        await pilot.pause()
        history_before = list(app.history)
        session_before = app.session.id if app.session else None
        await app.handle_input("/clear")
        await pilot.pause()

        content = "\n".join(str(widget.content) for widget in app.query("#messages Static").results(Static))
        assert "Workspace ready" in content
        assert "/resume" not in content
        assert app.history == history_before
        assert app.session is not None and app.session.id == session_before


async def test_model_command_lists_configured_models(tmp_path: Path) -> None:
    app = _make_multi_model_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.handle_input("/model")
        await pilot.pause()
        text = "\n".join(str(widget.content) for widget in app.query("#messages Static").results(Static))

    # Both models are listed with the active one marked.
    assert "alpha" in text
    assert "beta" in text
    assert "* alpha" in text  # active marker


async def test_model_command_switches_active_model(tmp_path: Path) -> None:
    app = _make_multi_model_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Wait for resource open to complete (it mounts resources.open()).
        await pilot.pause()
        await app.handle_input("/model beta")
        await pilot.pause()
        await pilot.pause()
        text = "\n".join(str(widget.content) for widget in app.query("#messages Static").results(Static))

    assert "Switched to beta" in text
    assert app.resources.active_model_name() == "beta"


async def test_model_command_rejects_unknown_name(tmp_path: Path) -> None:
    app = _make_multi_model_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.handle_input("/model gamma")
        await pilot.pause()
        text = "\n".join(str(widget.content) for widget in app.query("#messages Static").results(Static))

    assert "Unknown model 'gamma'" in text


async def test_cancel_run_clears_pending_approval_futures(tmp_path: Path) -> None:
    """``action_cancel_run`` resolves every pending approval as denied."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate the runtime awaiting an approval by allocating a waiter.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        app._approval_waiters["call-cancel"] = future  # type: ignore[attr-defined]
        app.action_cancel_run()
        await pilot.pause()
        assert app._approval_waiters == {}  # type: ignore[attr-defined]
        assert future.done()
        approval = future.result()
        assert approval.approved is False
    # No pending asyncio tasks should outlive the run.
    pending = asyncio.all_tasks()
    assert not any("agent-run" in str(t) for t in pending)


async def test_final_answer_does_not_contain_progress_text(tmp_path: Path) -> None:
    """The Markdown widget should not absorb progress or commentary text."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate the events the runtime emits.
        await app._render_event(ProgressReported(summary="found thing", next_action="next"))  # type: ignore[attr-defined]
        await app._render_event(CommentaryDelta("Intermediary musings."))  # type: ignore[attr-defined]
        await app._render_event(RunStarted("hi"))  # type: ignore[attr-defined]
        await app._render_event(TextDelta("Final answer only."))  # type: ignore[attr-defined]
        await pilot.pause()
        stream = app._assistant_stream  # type: ignore[attr-defined]
        assert stream is not None
        assistant_text = stream.text
    assert assistant_text == "Final answer only."


async def test_progress_uses_markdown_and_control_tools_stay_hidden(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.render_event(RunStarted("inspect"))
        await app.render_event(
            ToolCallStarted("control-1", "set_plan", {}, origin="control", risk="read")
        )
        await app.render_event(
            PlanCreated(PlanState(steps=[PlanStep(id="one", title="Inspect")]))
        )
        await app.render_event(ProgressReported(summary="**Step 1:** inspect"))
        await pilot.pause()

        assert len(app.query(ToolCard)) == 0
        progress = list(app.query(".progress-block").results(AssistantMarkdown))
        assert len(progress) == 1
        assert progress[0].source == "↳ **Step 1:** inspect"


async def test_inline_approval_replaces_modal(tmp_path: Path) -> None:
    """There is no longer an ApprovalModal; the modal class is gone entirely.

    This guards against accidentally restoring the old modal path during a
    future refactor.
    """
    # The approval module should no longer be importable.
    with pytest.raises(ModuleNotFoundError):
        import lumen.ui.approval  # type: ignore[import-not-found] # noqa: F401

    # And the app module should not reference it either.
    from lumen.ui import app as app_module

    assert not hasattr(app_module, "ApprovalModal")


# ---------------------------------------------------------------------------
# Streaming Markdown throttling
# ---------------------------------------------------------------------------


async def test_text_delta_buffers_until_flush(tmp_path: Path) -> None:
    """Each TextDelta appends to the buffer but does NOT immediately update
    the Markdown widget. The throttled flush (or a final RunCompleted flush)
    is what pushes the buffered text into the rendered markdown.
    """

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._render_event(RunStarted("hi"))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        # RunStarted no longer mounts an empty answer above subsequent tools;
        # the first text token lazily creates the segment in event order.
        assert app._assistant_container is None  # type: ignore[attr-defined]
        assert app._assistant_stream is None  # type: ignore[attr-defined]

        # Stream three tokens in quick succession. None should trigger a
        # synchronous update — the buffer just grows and a flush is scheduled.
        await app._render_event(TextDelta("Hello"))  # type: ignore[reportPrivateUsage]
        stream = app._assistant_stream  # type: ignore[attr-defined]
        assert stream is not None
        await app._render_event(TextDelta(", "))  # type: ignore[reportPrivateUsage]
        await app._render_event(TextDelta("world."))  # type: ignore[reportPrivateUsage]
        # The buffer reflects every token even though the markdown hasn't
        # been flushed yet.
        assert stream.text == "Hello, world."
        assert stream.pending is True

        # RunCompleted forces an immediate flush, clearing the dirty flag.
        await app._render_event(RunCompleted(output="done"))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        assert stream.pending is False


async def test_provisional_stream_is_reclassified_without_duplicate_answer(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.render_event(RunStarted("inspect"))
        await app.render_event(TextDelta("Inspecting."))
        await app._flush_assistant_now()  # type: ignore[reportPrivateUsage]
        assert [document.source for document in app.query(AssistantMarkdown)] == ["Inspecting."]

        await app.render_event(TextRetracted(len("Inspecting.")))
        await app.render_event(CommentaryDelta("Inspect"))
        await app.render_event(CommentaryDelta("ing."))
        await app.render_event(
            ToolCallStarted(
                call_id="read-1",
                name="read_file",
                args={"path": "README.md"},
                origin="builtin",
                risk="read",
            )
        )
        await app.render_event(TextDelta("Done."))
        await app.render_event(RunCompleted("Done."))
        await pilot.pause()

        assert [document.source for document in app.query(AssistantMarkdown)] == ["Done."]
        commentary = list(app.query(".commentary-block").results(Static))
        assert len(commentary) == 1
        assert str(commentary[0].content) == "∴ Inspecting."


async def test_blocking_clarification_renders_and_finishes_run_ui(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.render_event(RunStarted("choose target"))
        await app.render_event(
            ClarificationRequested(
                question_id="clarify-1",
                question="Choose a target",
                choices=("A", "B"),
            )
        )
        await app.render_event(
            RunWaitingForUser(
                question_id="clarify-1",
                question="Choose a target",
                choices=("A", "B"),
            )
        )
        await pilot.pause()

        messages = [str(widget.content) for widget in app.query(".system-message").results(Static)]
        assert any("Choose a target\n- A\n- B" in message for message in messages)
        assert "Waiting for your answer" in str(app.query_one("#status", Static).content)
        assert not app.query_one(RunActivityIndicator).has_class("running")


async def test_stale_file_completion_search_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lumen.ui import app as app_module

    old_started = threading.Event()
    release_old = threading.Event()

    def controlled_search(prefix: str, _workspace: Path) -> list[FileHit]:
        if prefix == "@old":
            old_started.set()
            release_old.wait(timeout=2)
            return [FileHit("old.py", "old.py", False, 80)]
        return [FileHit("new.py", "new.py", False, 80)]

    monkeypatch.setattr(app_module, "search_files", controlled_search)
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_task = asyncio.create_task(app._refresh_completions("@old"))  # type: ignore[attr-defined]
        assert await asyncio.to_thread(old_started.wait, 2)
        await app._refresh_completions("@new")  # type: ignore[attr-defined]
        release_old.set()
        await old_task

        dropdown = app.query_one("#completion-dropdown", CompletionDropdown)
        assert dropdown.trigger_prefix == "@new"
        assert [suggestion.label for suggestion in dropdown.suggestions] == ["new.py"]


async def test_mounted_timeline_widgets_remain_bounded(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        for index in range(250):
            await app._render_event(ProgressReported(summary=f"event {index}"))  # type: ignore[attr-defined]
        await pilot.pause()

        messages = app.query_one("#messages", VerticalScroll)
        assert len(messages.children) <= 202


async def test_new_activity_does_not_force_scroll_when_user_left_tail(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(80, 18)) as pilot:
        await pilot.pause()
        for index in range(40):
            await app._append_system(f"history {index}\nline two")  # type: ignore[attr-defined]
        messages = app.query_one("#messages", VerticalScroll)
        # Let the mounted history finish layout and tail following settle
        # before simulating an actual user scroll away from the bottom.
        await pilot.pause()
        messages.scroll_home(animate=False)
        await pilot.pause()
        before = messages.scroll_y

        await app._render_event(ProgressReported(summary="new event"))  # type: ignore[attr-defined]
        await pilot.pause()

        assert messages.scroll_y == before
        assert app.query_one("#new-activity", Static).has_class("visible")

        app.action_follow_tail()
        await pilot.pause()
        assert messages.is_vertical_scroll_end
        assert not app.query_one("#new-activity", Static).has_class("visible")


async def test_scrolling_restored_timeline_to_top_loads_older_page_without_jump(
    tmp_path: Path,
) -> None:
    app = _make_app(tmp_path)
    repository = app.resources.session_repository
    session = repository.create(agent_name="tui-test", model_id="test")
    for index in range(25):
        repository.append_turn(
            session.id,
            user_input=f"request {index}",
            messages=[
                ModelRequest(parts=[UserPromptPart(content=f"request {index}")]),
            ],
            approvals=[],
            usage={},
            status="completed",
        )
    app.resume_id = session.id

    async with app.run_test(size=(80, 18)) as pilot:
        await pilot.pause()
        messages = app.query_one("#messages", VerticalScroll)
        assert app.timeline_store.next_cursor == 5
        anchor = messages.children[0]
        messages.scroll_home(animate=False)
        await pilot.pause(0.01)
        anchor_y = anchor.region.y
        await app._load_older_if_at_top()  # type: ignore[attr-defined]
        await pilot.pause()

        assert app.timeline_store.next_cursor is None
        assert anchor in messages.children
        assert abs(anchor.region.y - anchor_y) <= 1


# ---------------------------------------------------------------------------
# PlanPanel conversation ownership
# ---------------------------------------------------------------------------


async def test_plan_panel_is_not_global_chrome(tmp_path: Path) -> None:
    """An empty app has no plan panel occupying fixed screen space."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(PlanPanel)) == 0


async def test_plan_created_event_mounts_panel_in_owning_turn(tmp_path: Path) -> None:
    """A plan is part of the scrolling transcript, not screen chrome."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._append_user("Do the first thing")  # type: ignore[reportPrivateUsage]
        await app._render_event(RunStarted("Do the first thing"))  # type: ignore[reportPrivateUsage]
        plan = PlanState(steps=[PlanStep(id="s1", title="Do thing", status=StepStatus.PENDING)])
        await app._render_event(PlanCreated(plan=plan))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        panel = app.query_one(PlanPanel)
        messages = app.query_one("#messages")
        assert panel.parent is messages
        assert list(messages.children).index(panel) > 0
        assert panel.has_class("has-plan")
        summary = _panel_summary(panel)
        assert "Do thing" in summary


async def test_new_turn_gets_a_distinct_plan_panel(tmp_path: Path) -> None:
    """Updating a later turn cannot move or replace an earlier turn's plan."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = PlanState(steps=[PlanStep(id="one", title="First plan")])
        second = PlanState(steps=[PlanStep(id="two", title="Second plan")])
        await app._render_event(RunStarted("First question"))  # type: ignore[reportPrivateUsage]
        await app._render_event(PlanCreated(first))  # type: ignore[reportPrivateUsage]
        await app._render_event(PlanUpdated(first))  # type: ignore[reportPrivateUsage]
        await app._render_event(RunStarted("Second question"))  # type: ignore[reportPrivateUsage]
        await app._render_event(PlanCreated(second))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        panels = list(app.query(PlanPanel))
        assert len(panels) == 2
        assert "First plan" in _panel_summary(panels[0])
        assert "Second plan" in _panel_summary(panels[1])


# ---------------------------------------------------------------------------
# Usage limit defaults
# ---------------------------------------------------------------------------


def test_limits_defaults_support_multi_step_plans(tmp_path: Path) -> None:
    """The default request_count must be high enough for an 8-step plan.

    An 8-step plan averages 2-3 LLM calls per step (planning + tool decision
    + summary), so 16-24 requests minimum. The old default of 12 was too low
    and caused real tasks to halt mid-way; 50 gives comfortable headroom.

    There is no ``total_tokens`` field at all (removed — context growth is
    managed by auto-compaction, mirroring coding-agent's design).
    """

    from lumen.config import LimitsConfig

    limits = LimitsConfig()
    assert limits.request_count >= 40, (
        f"request_count default {limits.request_count} is too low for multi-step plans"
    )
    assert limits.tool_calls >= 80
    # total_tokens field no longer exists on LimitsConfig.
    assert not hasattr(limits, "total_tokens")


# ---------------------------------------------------------------------------
# Status bar: mode + model suffix
# ---------------------------------------------------------------------------


async def test_status_bar_shows_mode_and_model(tmp_path: Path) -> None:
    """The bottom status bar always carries the approval mode + model id.

    The suffix persists across run-state changes (Thinking…, Ready, etc.) so
    the user always knows which model and mode are active — mirroring pi's
    footer. We check the idle state and after a simulated run.
    """

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        status_text = str(app.query_one("#status", Static).content)
        # Idle state shows mode and model without a misleading static ctx %.
        assert "manual" in status_text
        assert "test" in status_text  # model id from _make_app
        assert "ctx" not in status_text
        assert app.query_one("#status", Static).region.height == 1
        assert app.query_one("#topbar", Static).region.height == 1


async def test_idle_welcome_surfaces_runtime_context(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        welcome = app.query_one("#welcome", Static)
        content = str(welcome.content)

        # The brand is a real terminal-pixel wordmark, not a hidden text label.
        assert "▀" in content and "▄" in content
        assert "Workspace ready" in content
        assert "tools" in content and "skills" in content
        assert "commands" in content and "files" in content
        assert "Model" not in content
        assert "Mode" not in content
        assert str(tmp_path) not in content


async def test_status_bar_updates_on_mode_switch(tmp_path: Path) -> None:
    """Shift+Tab updates the single composer-adjacent mode indicator."""

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # First toggle enters accept-edits.
        await pilot.press("shift+tab")
        await pilot.pause()
        status_text = str(app.query_one("#status", Static).content)
        assert "accept edits on" in status_text
        accept_content = app.query_one("#status", Static).content
        assert isinstance(accept_content, Text)
        assert "bold #69B9AF" in {str(span.style) for span in accept_content.spans}
        await pilot.press("shift+tab")
        await pilot.pause()
        assert "plan mode on" in str(app.query_one("#status", Static).content)
        plan_content = app.query_one("#status", Static).content
        assert isinstance(plan_content, Text)
        assert "bold #93B97A" in {str(span.style) for span in plan_content.spans}
        await pilot.press("shift+tab")
        await pilot.pause()
        assert "auto" in str(app.query_one("#status", Static).content)
        await pilot.press("shift+tab")
        await pilot.pause()
        status_text = str(app.query_one("#status", Static).content)
        assert "manual" in status_text


async def test_shift_tab_cycles_approval_mode(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.approval_mode == "manual"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.approval_mode == "accept_edits"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.collaboration_mode == "plan"
        assert app.approval_mode == "accept_edits"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.approval_mode == "auto"
        assert "auto mode" in str(app.query_one("#status", Static).content)
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.approval_mode == "manual"


async def test_approval_selector_has_no_buttons(tmp_path: Path) -> None:
    """The pi-style approval selector uses no Button widgets.

    Guards against regressing back to the old Allow/Deny button pattern the
    user called jarring. The selector is a Static widget driven by keys.
    """

    from textual.widgets import Button as TextualButton

    from lumen.events import ToolApprovalPending

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.render_event(
            ToolCallStarted(
                "call-x",
                "write_file",
                {"path": "outputs/report.md"},
                origin="builtin",
                risk="write",
            )
        )
        await app.render_event(
            ToolApprovalPending(
                call_id="call-x",
                name="write_file",
                args={"path": "outputs/report.md"},
                origin="builtin",
                risk="write",
            )
        )
        await pilot.pause()
        panel = app.query_one(ApprovalPanel)
        prompt = app.query_one("#prompt")
        # The one interactive selector is a stable screen child immediately
        # above the prompt; the timeline card remains audit-only.
        assert panel.parent is app.screen
        assert panel.region.y + panel.region.height <= prompt.region.y
        assert panel.pending_count == 1
        assert list(app.query(".tool-approval").results()) == []
        buttons = list(panel.query(TextualButton).results())
        assert buttons == [], f"found unexpected buttons: {buttons}"
        # Global mode cycling remains available while the approval surface has
        # focus, matching Claude Code's composer-adjacent mode control.
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.approval_mode == "accept_edits"
        assert panel.pending_count == 1


async def test_activity_indicator_tracks_semantic_run_phase(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.render_event(RunStarted("Inspect the project"))
        await pilot.pause()
        activity = app.query_one(RunActivityIndicator)
        assert activity.has_class("running")
        assert "Thinking" in str(activity.content)

        await app.render_event(
            ToolCallStarted(
                "read-1",
                "read_file",
                {"path": "README.md"},
                origin="builtin",
                risk="read",
            )
        )
        await pilot.pause()
        assert "Reading" in str(activity.content)
        assert "README.md" in str(activity.content)

        await app.render_event(TextDelta("Done"))
        await pilot.pause()
        assert "Writing response" in str(activity.content)
        await app.render_event(RunCompleted("Done"))
        assert not activity.has_class("running")


# Unused imports kept for typing parity / future tests.
_ = (
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    CommentaryDelta,
    ContextCompactionStarted,
    ContextCompactionCompleted,
    RunStarted,
    RunCompleted,
    RunCancelled,
    TextDelta,
    ToolCallStarted,
    ToolCallFinished,
    ToolApprovalPending,
    ToolApprovalResolved,
    UsageUpdated,
    ModelMessage,
    ModelRequest,
    UserPromptPart,
    Any,
)
