"""Multi-model integration tests.

Covers ResourceManager.select_model (runtime rebuild), the CLI ``--model``
flag, and backwards compatibility with the single-model YAML form.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lumen.cli import app
from lumen.config import AgentSection, ModelSettingsConfig, load_config
from lumen.resources import ResourceManager


def _multi_model_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  default_model: alpha
  models:
    alpha: {id: test, api_key: k-alpha}
    beta: {id: test, api_key: k-beta}
tools:
  builtins: [read_file]
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# ResourceManager
# ---------------------------------------------------------------------------


async def test_resource_manager_default_model_picked_at_startup(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    assert manager.available_models() == ["alpha", "beta"]
    assert manager.active_model_name() == "alpha"

    async with manager:
        assert manager.runtime is not None
        assert manager.active_model_config().api_key == "k-alpha"
        summary = manager.summary()
        assert summary["active_model"] == "alpha"
        assert summary["available_models"] == ["alpha", "beta"]


async def test_select_model_rebuilds_runtime_and_switches_active(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        first_runtime = manager.runtime
        orchestrator = manager.agent_orchestrator
        assert first_runtime is not None
        await manager.select_model("beta")
        # The runtime instance changes because the Agent is rebuilt with the
        # new model bound at construction.
        assert manager.runtime is not first_runtime
        assert manager.runtime is not None
        assert manager.active_model_name() == "beta"
        assert manager.active_model_config().api_key == "k-beta"
        assert manager.agent_orchestrator is orchestrator


async def test_select_model_keeps_old_runtime_published_until_candidate_is_ready(
    tmp_path: Path,
) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        old_runtime = manager.runtime
        original_build = manager._build_runtime  # type: ignore[reportPrivateUsage]
        candidate_ready = asyncio.Event()
        release = asyncio.Event()

        async def delayed_build(*, for_name: str | None = None):  # type: ignore[no-untyped-def]
            candidate = await original_build(for_name=for_name)
            candidate_ready.set()
            await release.wait()
            return candidate

        manager._build_runtime = delayed_build  # type: ignore[method-assign]
        task = asyncio.create_task(manager.select_model("beta"))
        await candidate_ready.wait()

        assert manager.runtime is old_runtime
        assert manager.active_model_name() == "alpha"

        release.set()
        await task
        assert manager.runtime is not old_runtime
        assert manager.active_model_name() == "beta"


async def test_repeated_model_switches_keep_registrations_stable_and_quiesce_old_scope(
    tmp_path: Path,
) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        before = manager.registration_report()
        for name in ("beta", "alpha", "beta"):
            assert manager.runtime is not None
            engine = manager.runtime.context_engine
            assert engine is not None
            started = asyncio.Event()
            cleaned = asyncio.Event()

            async def compact(started: asyncio.Event, cleaned: asyncio.Event) -> None:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

            task = asyncio.create_task(compact(started, cleaned))
            engine._background_tasks["session"] = task  # pyright: ignore[reportPrivateUsage]
            await started.wait()
            await manager.select_model(name)
            assert task.cancelled()
            assert cleaned.is_set()
            assert engine._background_tasks == {}  # pyright: ignore[reportPrivateUsage]
        after = manager.registration_report()

        assert after["tool_count"] == before["tool_count"]
        assert after["hook_count"] == before["hook_count"]
        assert after["runtime_scope"]["closed"] is False
        assert after["cleanup_diagnostics"] == []


async def test_select_model_unknown_name_raises_keyerror(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        with pytest.raises(KeyError, match="unknown model"):
            await manager.select_model("does-not-exist")


async def test_select_model_noop_when_already_active(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        first_runtime = manager.runtime
        await manager.select_model("alpha")  # already active
        assert manager.runtime is first_runtime


async def test_select_model_before_open_just_sets_name(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    # Pre-open selection via the public CLI path: it must not try to rebuild a
    # runtime that doesn't exist yet.
    manager.set_startup_model("beta")
    async with manager:
        assert manager.active_model_name() == "beta"
        assert manager.active_model_config().api_key == "k-beta"


async def test_set_startup_model_rejects_unknown(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    with pytest.raises(KeyError, match="unknown model"):
        manager.set_startup_model("gamma")


async def test_set_startup_model_after_open_raises(tmp_path: Path) -> None:
    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        with pytest.raises(RuntimeError, match="before open"):
            manager.set_startup_model("beta")


async def test_single_model_form_loads_into_registry(tmp_path: Path) -> None:
    """Backwards compatibility: legacy `model:` form still works end-to-end."""

    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model: {id: test, api_key: k-single}
tools: {builtins: []}
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)
    # Registry holds a synthesised name derived from the model id.
    assert manager.available_models() == ["test"]
    assert manager.active_model_name() == "test"
    async with manager:
        assert manager.active_model_config().api_key == "k-single"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_model_flag_selects_startup_model(tmp_path: Path) -> None:
    config_path = _multi_model_config(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "--cwd", str(tmp_path), "--model", "beta", "--check-config"],
    )
    assert result.exit_code == 0, result.stdout
    assert '"active_model": "beta"' in result.stdout
    assert '"available_models": [' in result.stdout


def test_cli_model_flag_rejects_unknown_name(tmp_path: Path) -> None:
    config_path = _multi_model_config(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "--cwd",
            str(tmp_path),
            "--model",
            "gamma",
            "--check-config",
        ],
    )
    assert result.exit_code == 1
    # Typer writes the error to stderr via typer.echo(..., err=True).
    output = (result.stdout or "") + (result.output or "")
    assert "unknown model 'gamma'" in output


def test_cli_default_model_used_when_flag_absent(tmp_path: Path) -> None:
    config_path = _multi_model_config(tmp_path)
    result = CliRunner().invoke(app, ["--config", str(config_path), "--cwd", str(tmp_path), "--check-config"])
    assert result.exit_code == 0
    assert '"active_model": "alpha"' in result.stdout


async def test_select_model_failure_preserves_old_model_and_runtime(tmp_path: Path) -> None:
    """Transactional switch: if building the new runtime fails, the old model
    name AND the old runtime must survive so the app keeps working."""
    from unittest.mock import AsyncMock

    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        original_runtime = manager.runtime
        original_name = manager.active_model_name()
        assert original_runtime is not None

        # Make the NEXT _build_runtime fail (simulating a model that won't
        # construct). _rebuild_runtime_for builds the candidate runtime before
        # committing; the build is what we sabotage.
        manager._build_runtime = AsyncMock(side_effect=RuntimeError("model won't build"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="model won't build"):
            await manager.select_model("beta")

        # The old model name is unchanged...
        assert manager.active_model_name() == original_name
        # The published runtime was never replaced by the failed candidate.
        assert manager.runtime is original_runtime


async def test_apply_model_configuration_adds_inactive_model_without_rebuilding(
    tmp_path: Path,
) -> None:
    manager = ResourceManager(load_config(_multi_model_config(tmp_path)), workspace=tmp_path)
    async with manager:
        original_runtime = manager.runtime
        agent = AgentSection(
            models={
                **manager.model_registry,
                "gamma": ModelSettingsConfig(id="test", api_key="k-gamma"),
            },
            default_model="alpha",
        )

        await manager.apply_model_configuration(agent, active_model_name="alpha")

        assert manager.runtime is original_runtime
        assert manager.available_models() == ["alpha", "beta", "gamma"]
        assert manager.active_model_name() == "alpha"


async def test_apply_model_configuration_rebuilds_an_edited_active_model(
    tmp_path: Path,
) -> None:
    manager = ResourceManager(load_config(_multi_model_config(tmp_path)), workspace=tmp_path)
    async with manager:
        original_runtime = manager.runtime
        agent = AgentSection(
            models={
                "alpha": ModelSettingsConfig(id="test", api_key="updated-alpha"),
                "beta": manager.model_registry["beta"],
            },
            default_model="alpha",
        )

        await manager.apply_model_configuration(agent, active_model_name="alpha")

        assert manager.runtime is not original_runtime
        assert manager.active_model_config().api_key == "updated-alpha"


async def test_apply_model_configuration_failure_preserves_published_registry(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock

    manager = ResourceManager(load_config(_multi_model_config(tmp_path)), workspace=tmp_path)
    async with manager:
        original_runtime = manager.runtime
        original_registry = dict(manager.model_registry)
        agent = AgentSection(
            models={
                **manager.model_registry,
                "gamma": ModelSettingsConfig(id="test", api_key="k-gamma"),
            },
            default_model="gamma",
        )
        manager._build_runtime = AsyncMock(side_effect=RuntimeError("candidate failed"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="candidate failed"):
            await manager.apply_model_configuration(agent, active_model_name="gamma")

        assert manager.runtime is original_runtime
        assert manager.active_model_name() == "alpha"
        assert manager.model_registry == original_registry
