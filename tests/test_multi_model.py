"""Multi-model integration tests.

Covers ResourceManager.select_model (runtime rebuild), the CLI ``--model``
flag, and backwards compatibility with the single-model YAML form.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lumen.cli import app
from lumen.config import load_config
from lumen.resources import ResourceManager


def _multi_model_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 1
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
        assert first_runtime is not None
        await manager.select_model("beta")
        # The runtime instance changes because the Agent is rebuilt with the
        # new model bound at construction.
        assert manager.runtime is not first_runtime
        assert manager.runtime is not None
        assert manager.active_model_name() == "beta"
        assert manager.active_model_config().api_key == "k-beta"


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
version: 1
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
        # ...and the old runtime is still usable (restored, not None).
        assert manager.runtime is original_runtime


async def test_select_model_failure_restores_old_runtime_not_none(tmp_path: Path) -> None:
    """Even though the rebuild path detaches the old runtime before building, a
    failed build must restore the previous runtime rather than leave None."""
    from unittest.mock import AsyncMock

    config = load_config(_multi_model_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    async with manager:
        manager._build_runtime = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await manager.select_model("beta")
        # The app must not be left in a half-built state with no runtime.
        assert manager.runtime is not None
