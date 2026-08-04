from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.config import DelegationConfig, load_config
from lumen.delegation import DelegationManager
from lumen.resources import DELEGATION_TOOL_NAME, ResourceManager


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


async def test_delegated_agent_uses_read_tool_in_isolated_run() -> None:
    calls: list[str] = []

    def inspect_file(path: str) -> str:
        calls.append(path)
        return "evidence"

    async def model(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        returns = _tool_returns(messages)
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart("inspect_file", {"path": "README.md"}, tool_call_id="read")]
            )
        return ModelResponse(parts=[TextPart(f"finding: {returns[-1].content}")])

    manager = DelegationManager(
        model=FunctionModel(function=model),
        tools=[Tool(inspect_file)],
        config=DelegationConfig(enabled=True),
    )

    result = await manager.delegate_task("inspect the project")

    assert calls == ["README.md"]
    assert result == "finding: evidence"


async def test_delegation_concurrency_is_bounded() -> None:
    active = 0
    peak = 0

    async def model(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ModelResponse(parts=[TextPart("done")])

    manager = DelegationManager(
        model=FunctionModel(function=model),
        tools=[],
        config=DelegationConfig(enabled=True, max_concurrency=1),
    )

    assert await asyncio.gather(
        manager.delegate_task("first"),
        manager.delegate_task("second"),
    ) == ["done", "done"]
    assert peak == 1


async def test_delegation_rejects_empty_task() -> None:
    manager = DelegationManager(
        model=FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("unused")])),
        tools=[],
        config=DelegationConfig(enabled=True),
    )
    with pytest.raises(ValueError, match="must not be empty"):
        await manager.delegate_task("  ")


def test_delegation_config_is_bounded() -> None:
    with pytest.raises(ValidationError):
        DelegationConfig(enabled=True, max_concurrency=9)
    with pytest.raises(ValidationError):
        DelegationConfig(enabled=True, request_count=0)


async def test_resource_manager_exposes_delegation_only_when_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 1
agent: {model: {id: test}}
tools: {builtins: [read_file, write_file]}
delegation:
  enabled: true
  max_concurrency: 2
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.runtime is not None
        assert manager.delegation_manager is not None
        assert manager.tool_metadata[DELEGATION_TOOL_NAME] == {
            "origin": "control:delegation",
            "risk": "read",
            "control": "true",
        }
        schema_names = {item["name"] for item in manager.runtime.tool_schema_documents}
        assert DELEGATION_TOOL_NAME in schema_names
