"""Tests for the command palette (Ctrl+P) integration.

Covers the LumenCommandProvider: it must surface session/model/tool/run
commands, dynamically reflect configured models, and the commands it yields
must actually execute when invoked.
"""

from __future__ import annotations

from pathlib import Path

from lumen.config import load_config
from lumen.resources import ResourceManager
from lumen.ui.app import LumenApp
from lumen.ui.commands import LumenCommandProvider


def _multi_model_app(tmp_path: Path) -> LumenApp:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 1
agent:
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


# ---------------------------------------------------------------------------
# Provider unit tests (no TUI mount required for search logic)
# ---------------------------------------------------------------------------


async def test_provider_lists_all_command_categories(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        names = [name for name, _, _ in provider.commands]
        # Every category is represented.
        assert any(n.startswith("session:") for n in names)
        assert any(n.startswith("model:") for n in names)
        assert any(n.startswith("tools:") for n in names)
        assert "view: Clear timeline" in names
        assert any(n.startswith("run:") for n in names)
        assert any(n.startswith("approval:") for n in names)
        assert any(n.startswith("app:") for n in names)


async def test_provider_reflects_configured_models(tmp_path: Path) -> None:
    """Model commands are built live from the model registry."""

    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        model_cmds = [n for n, _, _ in provider.commands if n.startswith("model:")]
        assert "model: Switch to alpha" in model_cmds
        assert "model: Switch to beta" in model_cmds


async def test_search_finds_commands_by_prefix(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        hits = [h async for h in provider.search("model")]
        # Both model-switch commands match "model".
        assert len(hits) >= 2


async def test_search_returns_nothing_for_garbage(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        hits = [h async for h in provider.search("zzzznonexistent")]
        assert hits == []


async def test_discovery_yields_priority_commands(tmp_path: Path) -> None:
    """Empty-palette discovery shows the most useful starting points."""

    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        hits = [h async for h in provider.discover()]
        # The four priority commands are always discovered.
        assert len(hits) >= 4


async def test_quit_command_routes_through_project_safe_action(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        quit_command = next(runnable for name, runnable, _ in provider.commands if name == "app: Exit")

        assert quit_command == app.action_safe_quit


async def test_palette_quit_cancels_and_waits_for_active_worker(tmp_path: Path) -> None:
    events: list[str] = []

    class ActiveWorker:
        is_running = True

        def cancel(self) -> None:
            events.append("cancel")

        async def wait(self) -> None:
            events.append("wait")
            self.is_running = False

    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.current_worker = ActiveWorker()  # type: ignore[assignment]
        provider = LumenCommandProvider(screen=app.screen)
        quit_command = next(runnable for name, runnable, _ in provider.commands if name == "app: Exit")

        await quit_command()

    assert events == ["cancel", "wait"]


# ---------------------------------------------------------------------------
# Integration: commands actually execute
# ---------------------------------------------------------------------------


async def test_switch_model_command_changes_active_model(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.resources.active_model_name() == "alpha"
        provider = LumenCommandProvider(screen=app.screen)
        # Find the beta switch command and run it.
        beta_cmd = next(
            (runnable for name, runnable, _ in provider.commands if name == "model: Switch to beta")
        )
        await beta_cmd()
        await pilot.pause()
        assert app.resources.active_model_name() == "beta"


async def test_new_session_command_creates_session(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        original_session = app.session
        assert original_session is not None
        provider = LumenCommandProvider(screen=app.screen)
        new_cmd = next(runnable for name, runnable, _ in provider.commands if name == "session: New")
        await new_cmd()
        await pilot.pause()
        assert app.session is not None
        assert app.session.id != original_session.id


async def test_list_tools_command_outputs_tool_metadata(tmp_path: Path) -> None:
    app = _multi_model_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        provider = LumenCommandProvider(screen=app.screen)
        tools_cmd = next(runnable for name, runnable, _ in provider.commands if name == "tools: List visible")
        await tools_cmd()
        await pilot.pause()
        # The system-message area should now mention at least one control
        # tool name (set_plan / update_step / report_progress are always
        # registered). We read the Static widgets' content directly.
        from textual.widgets import Static

        statics = app.query("#messages Static")
        rendered = "\n".join(str(getattr(w, "content", "")) for w in statics.results(Static))
        assert any(t in rendered for t in ("set_plan", "report_progress", "update_step"))
