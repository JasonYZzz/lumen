from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from lumen.agents.compat import LegacyChildRunAdapter
from lumen.agents.orchestrator import AgentOrchestrator
from lumen.child_runs import ChildKind
from lumen.config import DelegationConfig, load_config
from lumen.resources import AGENT_TOOL_NAMES, CHILD_TOOL_NAMES, ResourceManager


@pytest.mark.parametrize(
    ("kind", "agent_type"),
    [(ChildKind.RESEARCH, "explorer"), (ChildKind.WORKTREE, "worker")],
)
async def test_legacy_spawn_child_is_a_thin_native_agent_alias(
    kind: ChildKind,
    agent_type: str,
) -> None:
    spawn = AsyncMock(
        return_value=json.dumps(
            {"ref": {"id": "agent-one"}, "status": "queued"},
        )
    )
    orchestrator = cast(AgentOrchestrator, SimpleNamespace(spawn_agent=spawn))
    adapter = LegacyChildRunAdapter(orchestrator)

    result = json.loads(await adapter.spawn_child("inspect the project", kind, "step-one"))

    assert result == {
        "child_id": "agent-one",
        "status": "queued",
        "kind": kind.value,
    }
    spawn.assert_awaited_once_with(
        "inspect the project",
        agent_type=agent_type,
        plan_step_id="step-one",
    )


def test_delegation_config_is_bounded() -> None:
    with pytest.raises(ValidationError):
        DelegationConfig(enabled=True, max_concurrency=9)
    with pytest.raises(ValidationError):
        DelegationConfig(enabled=True, request_count=0)


async def test_resource_manager_exposes_child_tools_only_when_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent: {model: {id: test}}
tools: {builtins: [read_file, write_file]}
sandbox: {mode: disabled}
delegation:
  enabled: true
  max_concurrency: 2
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.runtime is not None
        assert manager.child_run_manager is not None
        assert set(CHILD_TOOL_NAMES) <= set(manager.tool_metadata)
        schema_names = {item["name"] for item in manager.runtime.tool_schema_documents}
        assert set(CHILD_TOOL_NAMES) <= schema_names
        assert set(AGENT_TOOL_NAMES) <= schema_names


async def test_native_agent_tools_are_enabled_without_legacy_delegation(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent: {model: {id: test}}
tools: {builtins: [read_file]}
sandbox: {mode: disabled}
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.agents.enabled is True
    manager = ResourceManager(config, workspace=tmp_path)

    async with manager:
        assert manager.runtime is not None
        schema_names = {item["name"] for item in manager.runtime.tool_schema_documents}
        assert set(AGENT_TOOL_NAMES) <= schema_names
        assert not (set(CHILD_TOOL_NAMES) & schema_names)


def test_legacy_delegation_maps_to_agents_with_warning(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent: {model: {id: test}}
delegation:
  enabled: false
  max_concurrency: 2
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.agents.enabled is False
    assert config.agents.max_concurrency == 2
    assert "delegation is deprecated; use agents" in config.config_warnings
