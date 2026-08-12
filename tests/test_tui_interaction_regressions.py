from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from lumen.config import load_config
from lumen.events import (
    ApprovalRequest,
    PlanCreated,
    PlanReviewPending,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from lumen.interactive_queue import QueueMode
from lumen.plan import PlanState, PlanStep
from lumen.resources import ResourceManager
from lumen.run_coordinator import RunInput
from lumen.ui.app import LumenApp, PromptEditor
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.streaming_markdown import AssistantMarkdown
from lumen.ui.tool_card import ToolCard
from lumen.ui.welcome import WelcomePanel


def _app(tmp_path: Path) -> LumenApp:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  name: interaction-test
  model: {id: test}
tools: {builtins: []}
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


async def test_first_timeline_content_replaces_welcome_without_layout_residue(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(100, 30)) as pilot:
        assert len(list(app.query(WelcomePanel))) == 1

        await app._append_user("Inspect the current layout")  # type: ignore[reportPrivateUsage]
        await pilot.pause()

        messages = app.query_one("#messages", VerticalScroll)
        user_row = app.query_one(".user-message", Static)
        assert list(app.query(WelcomePanel)) == []
        assert len(messages.children) == 1
        assert "Inspect the current layout" in str(user_row.content)
        assert user_row.region.y - messages.content_region.y <= 2


async def test_run_started_dismisses_welcome_when_no_user_row_was_mounted(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)

    async with app.run_test() as pilot:
        await app.render_event(RunStarted("resume approved plan"))
        await pilot.pause()

        assert list(app.query(WelcomePanel)) == []


async def test_successful_foreground_run_does_not_mount_completion_toast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    notifications: list[str] = []

    def capture_notification(message: str, **_kwargs: object) -> None:
        notifications.append(message)

    monkeypatch.setattr(app, "notify", capture_notification)

    async with app.run_test() as pilot:
        await app.render_event(RunStarted("answer"))
        await app.render_event(TextDelta("Done."))
        await app.render_event(RunCompleted("Done."))
        await pilot.pause()

        assert notifications == []


async def test_auto_mode_does_not_prompt_for_classified_write_or_execute(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test():
        app.set_approval_mode("auto")

        assert app._should_auto_approve("write") is True  # type: ignore[reportPrivateUsage]
        assert app._should_auto_approve("execute") is True  # type: ignore[reportPrivateUsage]
        assert app._should_auto_approve("external_unknown") is False  # type: ignore[reportPrivateUsage]


async def test_mode_switch_does_not_retroactively_resolve_visible_approval(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    request = ApprovalRequest(
        call_id="write-1",
        name="write_file",
        args={"path": "outputs/report.html"},
        origin="builtin",
        risk="write",
    )

    async with app.run_test() as pilot:
        decision_task = app.run_worker(
            app._await_inline_approval(request),  # type: ignore[reportPrivateUsage]
            name="approval-test",
        )
        await pilot.pause()
        assert app.query_one(ApprovalPanel).pending_count == 1

        await pilot.press("shift+tab")
        await pilot.pause()

        assert app.approval_mode == "accept_edits"
        assert not decision_task.is_finished
        assert app.query_one(ApprovalPanel).pending_count == 1
        app._resolve_all_pending_approvals(  # type: ignore[reportPrivateUsage]
            approved=False, message="test cleanup"
        )
        await decision_task.wait()


async def test_switching_to_auto_keeps_unclassified_remote_approval_visible(tmp_path: Path) -> None:
    app = _app(tmp_path)
    request = ApprovalRequest(
        call_id="remote-1",
        name="remote_new_capability",
        args={},
        origin="mcp:remote",
        risk="external_unknown",
    )

    async with app.run_test() as pilot:
        decision_task = app.run_worker(
            app._await_inline_approval(request),  # type: ignore[reportPrivateUsage]
            name="unknown-approval-test",
        )
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        await pilot.press("shift+tab", "shift+tab")
        await pilot.pause()

        assert app.approval_mode == "auto"
        assert not decision_task.is_finished
        assert app.query_one(ApprovalPanel).pending_count == 1
        app._resolve_all_pending_approvals(  # type: ignore[reportPrivateUsage]
            approved=False, message="test cleanup"
        )
        await decision_task.wait()


async def test_final_text_segment_is_mounted_after_tool_cards(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        messages = app.query_one("#messages", VerticalScroll)
        await app.render_event(RunStarted("build report"))
        baseline = len(messages.children)
        await app.render_event(
            ToolCallStarted(
                "write-1",
                "write_file",
                {"path": "outputs/report.html"},
                origin="builtin",
                risk="write",
            )
        )
        await app.render_event(
            ToolCallFinished(
                "write-1",
                "write_file",
                "written",
                is_error=False,
                elapsed_seconds=0.1,
            )
        )
        await app.render_event(TextDelta("Report complete for 何氏眼科."))
        await app.render_event(RunCompleted("Report complete for 何氏眼科."))
        await pilot.pause()

        new_children = list(messages.children)[baseline:]
        tool_index = next(i for i, child in enumerate(new_children) if isinstance(child, ToolCard))
        answer_index = max(i for i, child in enumerate(new_children) if isinstance(child, AssistantMarkdown))
        assert tool_index < answer_index


async def test_completed_plan_opens_execution_mode_review(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.set_approval_mode("plan")
        await app.render_event(
            PlanCreated(
                PlanState(
                    goal="Inspect safely",
                    revision=1,
                    steps=[PlanStep(id="inspect", title="Inspect code")],
                )
            )
        )
        await app.render_event(TextDelta("Proposed implementation plan."))
        await app.render_event(PlanReviewPending(app.plan, 1))
        await app.render_event(RunCompleted("Proposed implementation plan."))
        await pilot.pause()

        panel = app.query_one(PlanReviewPanel)
        assert panel.has_class("visible")
        assert panel.region.y + panel.region.height <= app.query_one("#prompt").region.y

        await pilot.press("4")
        await pilot.pause()
        assert app.collaboration_mode == "plan"
        assert not panel.has_class("visible")


async def test_approving_plan_switches_mode_and_continues_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    submitted: list[str] = []

    async def capture_approval() -> None:
        submitted.append("approved")

    monkeypatch.setattr(app, "_run_approved_plan", capture_approval)
    async with app.run_test() as pilot:
        app.set_approval_mode("plan")
        await app.render_event(TextDelta("Plan proposal"))
        app.plan = PlanState(
            goal="Implement",
            revision=1,
            steps=[PlanStep(id="implement", title="Implement")],
        )
        await app.render_event(PlanReviewPending(app.plan, 1))
        await app.render_event(RunCompleted("Plan proposal"))
        await pilot.pause()

        await pilot.press("2")
        await pilot.pause()

        assert app.approval_mode == "accept_edits"
        assert submitted == ["approved"]


async def test_copy_latest_response_uses_textual_clipboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)

    def ignore_pbcopy(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr("lumen.ui.app.subprocess.run", ignore_pbcopy)
    async with app.run_test() as pilot:
        await app.render_event(RunCompleted("完整输出\n```python\nprint('ok')\n```"))
        await pilot.pause()

        app.action_copy_last_response()

        assert app.clipboard == "完整输出\n```python\nprint('ok')\n```"


async def test_stream_growth_keeps_tail_visible_when_following(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(80, 18)) as pilot:
        for index in range(35):
            await app._append_system(f"history {index}\nsecond line")  # type: ignore[reportPrivateUsage]
        messages = app.query_one("#messages", VerticalScroll)
        app.action_follow_tail()
        await pilot.pause()
        assert messages.is_vertical_scroll_end


async def test_failure_layout_shrink_keeps_error_at_tail(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(80, 18)) as pilot:
        for index in range(20):
            await app._append_system(f"history {index}\nsecond line")  # type: ignore[reportPrivateUsage]
        app.action_follow_tail()
        await pilot.pause()

        await app.render_event(RunStarted("fragile operation"))
        await app.render_event(TextDelta("\n\n".join(f"partial {index}" for index in range(30))))
        await app.render_event(RunFailed("provider interrupted"))
        await pilot.pause()

        messages = app.query_one("#messages", VerticalScroll)
        assert messages.is_vertical_scroll_end
        assert any(
            "Run failed: provider interrupted" in str(widget.content)
            for widget in app.query(".system-message").results(Static)
        )

        await app.render_event(RunStarted("long answer"))
        await app.render_event(TextDelta("\n\n".join(f"line {index}" for index in range(80))))
        await app.render_event(RunCompleted("done"))
        await pilot.pause()

        assert messages.is_vertical_scroll_end


async def test_long_markdown_answer_keeps_one_top_level_widget(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await app.render_event(RunStarted("long answer"))
        await app.render_event(TextDelta("\n\n".join(f"paragraph {i}" for i in range(250))))
        await app.render_event(RunCompleted("done"))
        await pilot.pause()

        documents = list(app.query(AssistantMarkdown).results(AssistantMarkdown))
        assert len(documents) == 1
        # Frozen blocks live inside the one document widget, not as siblings
        # in the timeline; the full source remains available on the document.
        assert documents[0].children
        assert documents[0].source.endswith("paragraph 249")


async def test_footer_keeps_last_usage_after_run_completed(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await app.render_event(
            UsageUpdated(
                usage={"input_tokens": 1200, "output_tokens": 345},
                request_count=3,
                tool_call_count=2,
                context_tokens_estimate=4096,
                elapsed_seconds=4.2,
            )
        )
        await app.render_event(RunCompleted("done"))
        await pilot.pause()

        status = str(app.query_one("#status", Static).content)
        assert "ctx 4.1k" in status
        assert "req 3" in status
        assert "tools 2" in status


@pytest.mark.parametrize("width", [60, 80, 120, 160])
async def test_responsive_status_and_cjk_emoji_rendering(tmp_path: Path, width: int) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(width, 24)) as pilot:
        await app.render_event(RunStarted("分析中文 🚀"))
        await app.render_event(TextDelta("## 结果\n\n中文内容 😀"))
        await app.render_event(RunCompleted("done"))
        await pilot.pause()

        status = str(app.query_one("#status", Static).content)
        documents = list(app.query(AssistantMarkdown).results(AssistantMarkdown))
        assert status.startswith("⏸ manual mode on")
        assert documents[-1].source.endswith("中文内容 😀")


async def test_manual_skill_timeline_label_keeps_user_arguments(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".lumen" / "skills" / "utb-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: utb-report\ndescription: Build a UTB report.\n---\nBuild the report.\n",
        encoding="utf-8",
    )
    app = _app(tmp_path)

    async with app.run_test() as pilot:
        await app.handle_input("/skill:utb-report 何氏眼科 2025年报")
        await pilot.pause()

        labels = [str(widget.content) for widget in app.query(".user-message").results(Static)]
        assert any("/skill:utb-report 何氏眼科 2025年报" in label for label in labels)


async def test_running_prompt_queues_steering_and_follow_up_without_losing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(tmp_path)

    class RunningWorker:
        is_running = True

        def cancel(self) -> None:
            self.is_running = False

        async def wait(self) -> None:
            return None

    captured: list[tuple[str, QueueMode]] = []

    async with app.run_test() as pilot:
        runtime = app.resources.runtime
        assert runtime is not None

        async def enqueue(run_input: RunInput, mode: QueueMode) -> object:
            captured.append((run_input.display_text, mode))
            return runtime.interactive_queue.enqueue(
                run_input.display_text,
                run_input.model_prompt,
                mode,
            )

        monkeypatch.setattr(app.coordinator, "enqueue_interactive", enqueue)
        app.current_worker = RunningWorker()  # type: ignore[assignment]
        editor = app.query_one("#prompt", PromptEditor)

        editor.text = "steer now"
        await pilot.press("enter")
        editor.text = "afterwards"
        await pilot.press("alt+enter")
        await pilot.pause()

        assert captured == [
            ("steer now", QueueMode.STEER),
            ("afterwards", QueueMode.FOLLOW_UP),
        ]
        assert editor.text == ""
        assert app.query_one("#interactive-queue", Static).has_class("visible")
