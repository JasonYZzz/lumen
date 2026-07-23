from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from lumen.config import load_config
from lumen.events import (
    ContextCompactionCompleted,
    ContextCompactionStarted,
    PlanCreated,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolApprovalPending,
    ToolCallFinished,
    ToolCallStarted,
)
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.resources import ResourceManager
from lumen.ui.activity_indicator import RunActivityIndicator
from lumen.ui.app import LumenApp
from lumen.ui.welcome import WelcomePanel

STATES = (
    "idle",
    "slash-menu",
    "slash-filter",
    "loading",
    "todo",
    "stream",
    "code-block",
    "tool",
    "tool-answer",
    "edit-file",
    "error",
    "approval",
    "compaction",
)
SIZES = ((80, 24), (120, 36))
THEMES = ("lumen-dark", "lumen-light")


def _snapshot_app(tmp_path: Path) -> LumenApp:
    # Keep the model-invocable skill tools deterministic instead of depending
    # on whichever user-global skills happen to exist on the test machine.
    skill_dir = tmp_path / ".lumen" / "skills" / "snapshot"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: snapshot\ndescription: Stable snapshot fixture.\n---\nFixture body.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 1
agent:
  name: snapshot-agent
  model: {id: test}
tools: {builtins: []}
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("terminal_size", SIZES)
def test_tui_state_snapshot(
    snap_compare: Callable[..., bool],
    tmp_path: Path,
    state: str,
    theme: str,
    terminal_size: tuple[int, int],
) -> None:
    app = _snapshot_app(tmp_path)

    async def arrange(pilot: Pilot[Any]) -> None:
        pilot.app.theme = theme
        # Snapshot fixtures must not include per-run UUIDs or pytest's
        # generated temporary path. Dynamic-value behaviour is covered by
        # integration tests; these images guard the visual contract.
        app.query_one("#topbar", Static).update(
            "◆ Lumen / snapshot-agent  ·  model test  ·  manual mode  ·  session a1b2c3d4"
        )
        app.query_one(WelcomePanel).update_context(
            agent_name="snapshot-agent",
            model="test",
            mode="manual",
            session_id="a1b2c3d4",
            workspace=Path("~/projects/lumen"),
            tool_count=4,
            skill_count=2,
            mcp_summary="no MCP servers",
        )
        app.query_one(RunActivityIndicator).set_animation_enabled(False)
        if state == "idle":
            return
        if state in {"slash-menu", "slash-filter"}:
            editor = app.query_one("#prompt")
            editor.focus()
            keys = "/" if state == "slash-menu" else "/qu"
            for key in keys:
                await pilot.press(key)
        elif state == "loading":
            await app.render_event(RunStarted("Inspect the project before making changes"))
            await app.render_event(
                ToolCallStarted(
                    "snapshot-loading",
                    "read_file",
                    {"path": "src/lumen/ui/app.py"},
                    origin="builtin",
                    risk="read",
                )
            )
        elif state == "todo":
            await app.render_event(
                PlanCreated(
                    PlanState(
                        steps=[
                            PlanStep(
                                id="one",
                                title="Inspect current behavior",
                                status=StepStatus.COMPLETED,
                            ),
                            PlanStep(
                                id="two",
                                title="Implement interaction",
                                status=StepStatus.IN_PROGRESS,
                            ),
                            PlanStep(
                                id="three",
                                title="Run visual regression",
                                status=StepStatus.PENDING,
                            ),
                        ]
                    )
                )
            )
        elif state == "stream":
            await app.render_event(RunStarted("Explain the architecture"))
            await app.render_event(TextDelta("## Architecture\n\nThe agent is streaming **Markdown**."))
            await app.render_event(RunCompleted("done"))
        elif state == "code-block":
            # A fenced code block exercises Textual's pygments-based code
            # highlighting (MarkdownFence) and locks the highlighted visual
            # contract for both themes.
            await app.render_event(RunStarted("Show an example"))
            await app.render_event(
                TextDelta(
                    "Here is an example:\n\n"
                    "```python\n"
                    "def greet(name: str) -> str:\n"
                    "    # return a greeting\n"
                    "    return f'hello, {name}'\n"
                    "```\n"
                )
            )
            await app.render_event(RunCompleted("done"))
        elif state == "tool":
            await app.render_event(RunStarted("Inspect README.md"))
            await app.render_event(
                ToolCallStarted(
                    "snapshot-tool",
                    "read_file",
                    {"path": "README.md", "encoding": "utf-8"},
                    origin="builtin",
                    risk="read",
                )
            )
            await app.render_event(
                ToolCallFinished(
                    "snapshot-tool",
                    "read_file",
                    "Full file result that remains available when expanded.",
                    is_error=False,
                    elapsed_seconds=0.12,
                    preview="README preview…",
                )
            )
        elif state == "tool-answer":
            await app.render_event(RunStarted("Generate a report"))
            await app.render_event(
                ToolCallStarted(
                    "snapshot-report",
                    "write_file",
                    {"path": "outputs/report.html"},
                    origin="builtin",
                    risk="write",
                )
            )
            await app.render_event(
                ToolCallFinished(
                    "snapshot-report",
                    "write_file",
                    "Report written.",
                    is_error=False,
                    elapsed_seconds=0.42,
                    preview="outputs/report.html",
                )
            )
            await app.render_event(
                TextDelta("Report complete for **Example Company**.\n\nSaved to `outputs/report.html`.")
            )
            await app.render_event(RunCompleted("done"))
        elif state == "edit-file":
            # edit_file renders a find -> replace diff in the card body instead
            # of raw JSON args; this locks the diff visual contract.
            await app.render_event(RunStarted("Fix the function name"))
            await app.render_event(
                ToolCallStarted(
                    "snapshot-edit",
                    "edit_file",
                    {
                        "path": "src/app.py",
                        "find": "def gret(name):\n    return name",
                        "replace": "def greet(name):\n    return name",
                    },
                    origin="builtin",
                    risk="write",
                )
            )
            await app.render_event(
                ToolCallFinished(
                    "snapshot-edit",
                    "edit_file",
                    "Edited src/app.py",
                    is_error=False,
                    elapsed_seconds=0.08,
                    preview="src/app.py",
                )
            )
        elif state == "error":
            await app.render_event(RunStarted("Run a fragile operation"))
            await app.render_event(TextDelta("Partial output before failure."))
            await app.render_event(RunFailed("Provider connection was interrupted"))
        elif state == "approval":
            await app.render_event(
                ToolCallStarted(
                    "snapshot-approval",
                    "write_file",
                    {"path": "outputs/report.md", "content": "hello"},
                    origin="builtin",
                    risk="write",
                )
            )
            await app.render_event(
                ToolApprovalPending(
                    "snapshot-approval",
                    "write_file",
                    {"path": "outputs/report.md", "content": "hello"},
                    origin="builtin",
                    risk="write",
                )
            )
        elif state == "compaction":
            await app.render_event(ContextCompactionStarted(source_message_count=84))
            await app.render_event(
                ContextCompactionCompleted(active_message_count=18, summary_tokens_estimate=1_240)
            )
        await pilot.pause()

    assert snap_compare(
        app,
        terminal_size=terminal_size,
        run_before=arrange,
    )
