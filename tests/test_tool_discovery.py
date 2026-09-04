from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ToolReturnPart, ToolSearchReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import LimitsConfig, PermissionsConfig
from lumen.events import RunEvent
from lumen.runtime import AgentRuntime, ToolApproval
from lumen.tools.gateway import CapabilityDescriptor, CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind


async def test_discovery_recovers_from_language_mismatch_pages_and_preserves_approval(tmp_path: Path) -> None:
    gateway = CapabilityGateway(
        ToolRegistry(tmp_path), PermissionPolicy(PermissionsConfig()), default_timeout=5,
    )
    calls: list[dict[str, Any]] = []

    async def invoke(arguments: dict[str, Any]) -> str:
        calls.append(arguments)
        return "must not execute after denial"

    allowed = [*(f"a_tool_{index:02}" for index in range(12)), "exa_web_search"]
    for name in [*allowed, "parent_only_private_tool"]:
        gateway.register(
            CapabilityDescriptor(
                name=name,
                description="Search the web." if name == "exa_web_search" else "Other remote capability.",
                parameters={"type": "object", "properties": {}},
                origin="mcp:example",
                risk="external_unknown",
                effect_kind=EffectKind.UNKNOWN,
                requires_approval=True,
                timeout_seconds=5,
                deferred=True,
            ),
            invoke,
        )

    requests = 0

    async def model_stream(
        messages: list[ModelMessage], info: AgentInfo,
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        nonlocal requests
        requests += 1
        names = {tool.name for tool in info.function_tools}
        assert "parent_only_private_tool" not in names
        assert all("parent_only_private_tool" not in (tool.description or "") for tool in info.function_tools)
        search_results = [
            part for message in messages for part in message.parts if isinstance(part, ToolSearchReturnPart)
        ]
        if requests == 1:
            search = next(tool for tool in info.function_tools if tool.name == "search_tools")
            assert "exa_web_search" in (search.description or "")
            assert "exa_web_search" not in names
            yield {0: DeltaToolCall("search_tools", '{"queries":["在线查询"]}', tool_call_id="search1")}
        elif requests == 2:
            assert not search_results[-1].content["discovered_tools"]
            yield {0: DeltaToolCall("search_tools", '{"queries":[""]}', tool_call_id="search2")}
        elif requests == 3:
            assert len(search_results[-1].content["discovered_tools"]) == 10
            assert "exa_web_search" not in names
            yield {0: DeltaToolCall("search_tools", '{"queries":[""]}', tool_call_id="search3")}
        elif requests == 4:
            assert len(search_results[-1].content["discovered_tools"]) == 3
            assert "exa_web_search" in names
            assert "search_tools" not in names
            yield {0: DeltaToolCall("exa_web_search", "{}", tool_call_id="invoke")}
        else:
            result = next(
                part for message in reversed(messages) for part in message.parts
                if isinstance(part, ToolReturnPart)
            )
            assert result.outcome == "denied"
            yield "The configured tool requires approval, and the request was denied."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_stream),
        tools=[], toolsets=[], instructions="Research using available tools.", limits=LimitsConfig(),
        tool_metadata={}, capability_gateway=gateway.narrow(allowed),
    )
    approvals = 0

    async def approve(_request: Any) -> ToolApproval:
        nonlocal approvals
        approvals += 1
        return ToolApproval(approved=False, message="User denied the action.")

    async def emit(_event: RunEvent) -> None:
        pass

    outcome = await runtime.run("请联网查询", [], emit, approve)
    assert "denied" in outcome.output
    assert approvals == 1
    assert calls == []
    assert requests == 5
