from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import (
    HookConfig,
    LimitsConfig,
    McpServerConfig,
    OAuthConfig,
    PermissionsConfig,
    load_config,
)
from lumen.context.assembler import ContextAssembler
from lumen.context.budget import ConservativeTokenCounter
from lumen.context.types import ContextZone, TrustLevel
from lumen.events import ApprovalRequest, RunEvent, ToolApprovalBatchPending, ToolApprovalResolved
from lumen.hooks import (
    CommandHookRunner,
    HookBus,
    HookDecision,
    HookEvent,
    PythonHookRunner,
    RegisteredHook,
)
from lumen.mcp_oauth import JsonCredentialStore
from lumen.mcp_resources import McpContentRegistry
from lumen.resources import ResourceManager
from lumen.runtime import AgentRuntime, ToolApproval
from lumen.skills import SkillLoader, load_skill
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import Risk, ToolConcurrency, ToolSpec


def _last_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _manager_config(tmp_path: Path, extra: str = "") -> Any:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """
version: 2
agent:
  model:
    id: test
tools:
  builtins: []
sessions:
  directory: sessions
"""
        + extra,
        encoding="utf-8",
    )
    return load_config(path)


def test_parallel_modes_set_per_tool_sequential_flag(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    registry.add(
        ToolSpec(
            lambda: "read",
            name="read",
            risk=Risk.READ,
            concurrency=lambda _args: ToolConcurrency.PARALLEL_SAFE,
        ),
        origin="test",
    )
    registry.add(ToolSpec(lambda: "write", name="write", risk=Risk.WRITE), origin="test")
    policy = PermissionPolicy(PermissionsConfig(always_allow=["write"]))

    safe = {
        tool.name: tool
        for tool in registry.build_local_tools(policy, default_timeout=1, parallel_mode="parallel_safe")
    }
    full = {
        tool.name: tool
        for tool in registry.build_local_tools(policy, default_timeout=1, parallel_mode="parallel")
    }

    assert safe["read"].sequential is False
    assert safe["write"].sequential is True
    assert all(tool.sequential is False for tool in full.values())


async def test_parallel_safe_runtime_overlaps_read_tools() -> None:
    order: list[str] = []
    both_started = asyncio.Event()
    starts = 0

    async def read_value(value: str) -> str:
        nonlocal starts
        order.append(f"start:{value}")
        starts += 1
        if starts == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        order.append(f"end:{value}")
        return value

    async def model(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if not _last_returns(messages):
            yield {
                0: DeltaToolCall("read_value", '{"value":"a"}', tool_call_id="a"),
                1: DeltaToolCall("read_value", '{"value":"b"}', tool_call_id="b"),
            }
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model),
        tools=[Tool(read_value, sequential=False)],
        toolsets=[],
        instructions="test",
        limits=LimitsConfig(parallel_tool_calls="parallel_safe"),
        tool_metadata={"read_value": {"origin": "test", "risk": "read"}},
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: ApprovalRequest) -> ToolApproval:
        raise AssertionError("read tool should not require approval")

    await runtime.run("go", [], emit, approve)
    assert set(order[:2]) == {"start:a", "start:b"}


async def test_parallel_approvals_use_one_batch_handler() -> None:
    def write_value(value: str) -> str:
        return value

    async def model(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if not _last_returns(messages):
            yield {
                0: DeltaToolCall("write_value", '{"value":"a"}', tool_call_id="a"),
                1: DeltaToolCall("write_value", '{"value":"b"}', tool_call_id="b"),
            }
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model),
        tools=[Tool(write_value, sequential=False, requires_approval=True)],
        toolsets=[],
        instructions="test",
        limits=LimitsConfig(parallel_tool_calls="parallel"),
        tool_metadata={"write_value": {"origin": "test", "risk": "write"}},
    )
    batches: list[tuple[ApprovalRequest, ...]] = []
    events: list[RunEvent] = []

    async def approve(_request: ApprovalRequest) -> ToolApproval:
        raise AssertionError("batch callback should be used")

    async def approve_batch(requests: tuple[ApprovalRequest, ...]) -> dict[str, ToolApproval]:
        batches.append(requests)
        return {request.call_id: ToolApproval(True, "batch") for request in requests}

    async def emit(event: RunEvent) -> None:
        events.append(event)

    await runtime.run("go", [], emit, approve, approve_batch)
    assert len(batches) == 1
    assert {request.call_id for request in batches[0]} == {"a", "b"}
    assert len([event for event in events if isinstance(event, ToolApprovalResolved)]) == 2


def test_hook_config_requires_exactly_one_runner() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        HookConfig(event="stop")
    with pytest.raises(ValidationError, match="exactly one"):
        HookConfig(event="stop", command=["true"], module="hooks")


async def test_command_hook_reads_json_and_can_deny(tmp_path: Path) -> None:
    script = tmp_path / "deny.py"
    script.write_text(
        "import json,sys\nctx=json.load(sys.stdin)\n"
        "print(ctx['tool_name'], file=sys.stderr)\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    runner = CommandHookRunner((sys.executable, str(script)), timeout=2)
    decision = await runner.run(
        HookBus(tmp_path).context(HookEvent.PRE_TOOL_USE, tool_name="run_command", tool_args={})
    )
    assert decision.allow is False
    assert decision.reason == "run_command"


async def test_hook_bus_matches_and_short_circuits(tmp_path: Path) -> None:
    calls: list[str] = []

    def allow(_context: object) -> HookDecision:
        calls.append("allow")
        return HookDecision()

    def deny(_context: object) -> HookDecision:
        calls.append("deny")
        return HookDecision(False, reason="blocked")

    def never(_context: object) -> HookDecision:
        calls.append("never")
        return HookDecision()

    bus = HookBus(tmp_path)
    bus.hooks.extend(
        [
            RegisteredHook(HookEvent.PRE_TOOL_USE, "read_*", PythonHookRunner(allow)),
            RegisteredHook(HookEvent.PRE_TOOL_USE, "read_file", PythonHookRunner(deny)),
            RegisteredHook(HookEvent.PRE_TOOL_USE, "*", PythonHookRunner(never)),
        ]
    )
    result = await bus.dispatch(bus.context(HookEvent.PRE_TOOL_USE, tool_name="read_file"))
    assert result.allow is False
    assert calls == ["allow", "deny"]


async def test_runtime_hooks_deny_tool_without_executing(tmp_path: Path) -> None:
    executed = False

    def echo(value: str) -> str:
        nonlocal executed
        executed = True
        return value

    def deny(_context: object) -> HookDecision:
        return HookDecision(False, reason="policy")

    bus = HookBus(tmp_path)
    bus.hooks.append(RegisteredHook(HookEvent.PRE_TOOL_USE, "echo", PythonHookRunner(deny)))

    async def model(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        returns = _last_returns(messages)
        if not returns:
            yield {0: DeltaToolCall("echo", '{"value":"x"}', tool_call_id="x")}
        else:
            yield str(returns[-1].content)

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model),
        tools=[Tool(echo)],
        toolsets=[],
        instructions="test",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
        hooks=bus,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: ApprovalRequest) -> ToolApproval:
        return ToolApproval(True)

    outcome = await runtime.run("go", [], emit, approve)
    assert executed is False
    assert "ToolDenied: policy" in outcome.output


def test_skill_scripts_are_confined_and_filtered(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "ok.sh").write_text("echo ok\n", encoding="utf-8")
    (skill_dir / "bad.exe").write_text("bad", encoding="utf-8")
    path = skill_dir / "SKILL.md"
    path.write_text(
        """---
name: scripted
description: scripted skill
scripts:
  ok: scripts/ok.sh
  escape: ../../outside.py
  bad: bad.exe
---
Run it.
""",
        encoding="utf-8",
    )
    warnings: list[str] = []
    skill = load_skill(path, "project", warnings)
    assert skill is not None
    assert set(skill.scripts) == {"ok"}
    assert len(warnings) == 2


def test_run_skill_script_sanitizes_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_dir = tmp_path / ".lumen" / "skills" / "scripted"
    (skill_dir / "scripts").mkdir(parents=True)
    script = skill_dir / "scripts" / "check.py"
    script.write_text(
        "import os\nprint('secret=' + str('SECRET_API_KEY' in os.environ))\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "---\nname: scripted\ndescription: test\nscripts:\n  check: scripts/check.py\n---\nrun\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SECRET_API_KEY", "do-not-leak")
    manager = ResourceManager(_manager_config(tmp_path), workspace=tmp_path)
    runner = manager.registry.entries["run_skill_script"].spec.function
    output = runner("scripted", "check")
    assert "exit_code: 0" in output
    assert "secret=False" in output


def test_builtin_skills_are_packaged_and_opt_in(tmp_path: Path) -> None:
    names = {skill.name for skill in SkillLoader(tmp_path, include_builtin=True).discover()}
    assert {"commit", "test-runner", "review-pr"}.issubset(names)


async def test_json_credential_store_persists_with_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "oauth.json"
    store = JsonCredentialStore(path)
    await store.put("token", {"access_token": "secret"}, collection="oauth")
    assert await store.get("token", collection="oauth") == {"access_token": "secret"}
    assert os.stat(path).st_mode & 0o777 == 0o600
    reloaded = JsonCredentialStore(path)
    assert await reloaded.get("token", collection="oauth") == {"access_token": "secret"}


class _FakeMcpClient:
    async def list_resources(self) -> list[dict[str, object]]:
        return [{"uri": "docs://schema", "name": "schema", "description": "Database schema"}]

    async def list_prompts(self) -> list[dict[str, object]]:
        return [{"name": "explain", "description": "Explain a topic", "arguments": [{"name": "topic"}]}]

    async def read_resource(self, uri: str) -> str:
        return f"resource:{uri}"

    async def get_prompt(self, name: str, arguments: dict[str, str] | None) -> dict[str, object]:
        assert arguments is not None
        return {"messages": [{"content": {"text": f"{name}:{arguments['topic']}"}}]}


async def test_mcp_resources_and_prompts_are_explicitly_loaded() -> None:
    registry = McpContentRegistry()
    bundle = cast(Any, SimpleNamespace(name="db", client=_FakeMcpClient()))
    await registry.add_server(
        bundle,
        McpServerConfig(transport="stdio", command="server"),
    )
    assert registry.resources[0].reference == "db::docs://schema"
    document = await registry.activate_resource("db::docs://schema")
    assert document["body"] == "resource:docs://schema"
    assert await registry.render_prompt("db:explain", {"topic": "indexes"}) == "explain:indexes"


def test_retrieved_mcp_content_has_its_own_untrusted_zone() -> None:
    assembler = ContextAssembler(
        window_tokens=10_000,
        max_output_tokens=500,
        counter=ConservativeTokenCounter(),
    )
    assembled = assembler.assemble(
        instructions="system",
        prompt="question",
        tool_schemas=[],
        history=[],
        retrieved_context=[{"server": "db", "uri": "docs://schema", "body": "CREATE TABLE users"}],
    )
    block = next(item for item in assembled.blocks if item.zone is ContextZone.RETRIEVED_CONTEXT)
    assert block.trust is TrustLevel.UNTRUSTED_EXTERNAL


def test_oauth_is_rejected_for_stdio() -> None:
    with pytest.raises(ValidationError, match="OAuth requires"):
        McpServerConfig(
            transport="stdio",
            command="server",
            oauth=OAuthConfig(client_id="client"),
        )


def test_batch_event_round_trips_requests() -> None:
    request = ApprovalRequest("one", "write", {"path": "a"}, "test", "write")
    event = ToolApprovalBatchPending("batch", (request,), "1xwrite")
    from lumen.events import TimelineEventRecord

    restored = TimelineEventRecord.from_event(event, sequence=1).to_event()
    assert restored == event
