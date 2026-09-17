from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lumen.config import load_config
from lumen.resources import ResourceManager
from lumen.tools.gateway import CapabilityDescriptor, CapabilityInvocation, CapabilityStatus
from lumen.tools.spec import EffectKind
from lumen.work_products import EffectStatus


def _manager(tmp_path: Path) -> tuple[ResourceManager, str]:
    config = tmp_path / "agent.yaml"
    config.write_text("version: 2\nagent: {model: {id: test}}\ntools: {builtins: []}\n")
    manager = ResourceManager(load_config(config), workspace=tmp_path)
    session = manager.session_repository.create(agent_name="test", model_id="test")
    manager.bind_session_context(session.id)
    return manager, session.id


@pytest.mark.parametrize("effect", [EffectKind.UNKNOWN, EffectKind.OBSERVE])
async def test_read_risk_does_not_replace_effect_preflight(tmp_path: Path, effect: EffectKind) -> None:
    manager, session_id = _manager(tmp_path)
    calls = 0

    async def search(_arguments: dict[str, object]) -> str:
        nonlocal calls
        calls += 1
        return "source-backed result"

    manager.capability_gateway.register(CapabilityDescriptor(
        name="exa_search", origin="mcp:exa", risk="read", effect_kind=effect,
        parameters={"type": "object", "properties": {}}, timeout_seconds=1,
    ), search)
    # The run-local overlay follows exactly the same preflight as Live/main.
    gateway = manager.capability_gateway.derive([])
    result = await gateway.invoke(CapabilityInvocation(
        execution_id="run", provider_call_id="call", name="exa_search",
    ))
    if effect is EffectKind.UNKNOWN:
        assert result.status is CapabilityStatus.DENIED
        assert "effect_contract_required" in (result.error or "")
        assert calls == 0
    else:
        assert result.status is CapabilityStatus.SUCCEEDED
        assert calls == 1
    assert manager.task_workspace.state_for(session_id).effects == ()
    assert manager.completion_issues(session_id) == ()


@pytest.mark.parametrize("effect_kind", [EffectKind.EXTERNAL_ACTION, EffectKind.EXECUTION])
@pytest.mark.parametrize("ending", ["success", "error", "timeout", "cancel"])
async def test_external_receipt_is_durable_before_dispatch_and_reconciles_uncertainty(
    tmp_path: Path, ending: str, effect_kind: EffectKind,
) -> None:
    manager, session_id = _manager(tmp_path)
    started = asyncio.Event()
    calls = 0

    async def remote(_arguments: dict[str, object]) -> str:
        nonlocal calls
        calls += 1
        saved = manager.session_repository.load(session_id).work_state.effects
        assert len(saved) == 1
        assert saved[0].status is EffectStatus.PREPARED
        started.set()
        if ending == "error":
            raise RuntimeError("failed after dispatch")
        if ending != "success":
            await asyncio.Event().wait()
        return "accepted"

    manager.capability_gateway.register(CapabilityDescriptor(
        name="remote_update", origin="mcp:remote", risk="write", effect_kind=effect_kind,
        parameters={"type": "object", "properties": {}}, timeout_seconds=0.05,
    ), remote)
    invocation = CapabilityInvocation(
        execution_id="run", provider_call_id="call", name="remote_update",
    )
    task = asyncio.create_task(manager.capability_gateway.invoke(invocation))
    await asyncio.wait_for(started.wait(), 1)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.succeeded is (ending == "success")
    effects = manager.session_repository.load(session_id).work_state.effects
    assert len(effects) == 1
    assert effects[0].status is (
        EffectStatus.VERIFIED if ending == "success" else EffectStatus.RECONCILIATION_REQUIRED
    )
    manager.task_workspace.bind_session(session_id)
    blockers = manager.task_workspace.completion_blockers(session_id)
    assert bool(blockers) is (ending != "success")
    if blockers:
        assert not blockers[0].model_recoverable
    replay = await manager.capability_gateway.invoke(invocation)
    assert replay.succeeded is (ending == "success")
    assert calls == 1


async def test_historical_failed_execution_requires_explicit_recovery(tmp_path: Path) -> None:
    manager, session_id = _manager(tmp_path)
    receipt = manager.task_workspace.record_tool_effect(
        tool_name="run_command", effect_kind=EffectKind.EXECUTION,
        success=False, summary="old failed command without a prepared receipt",
    )
    assert receipt is not None
    assert receipt.status is EffectStatus.FAILED
    assert receipt.after is None
    blockers = manager.task_workspace.completion_blockers(session_id)
    assert len(blockers) == 1
    assert not blockers[0].model_recoverable
    # An unrelated successful command cannot verify the failed invocation.
    manager.task_workspace.record_tool_effect(
        tool_name="run_command", effect_kind=EffectKind.EXECUTION,
        success=True, summary="later successful command",
    )
    assert len(manager.task_workspace.completion_blockers(session_id)) == 1
    assert manager.task_workspace.waive_effects(session_id, (receipt.id,), "operator inspected outputs") == 1
    assert manager.task_workspace.completion_blockers(session_id) == []


async def test_denied_execution_does_not_create_an_uncertain_receipt(tmp_path: Path) -> None:
    manager, session_id = _manager(tmp_path)

    async def execute(_arguments: dict[str, object]) -> str:
        raise AssertionError("denied command must not execute")

    manager.capability_gateway.register(CapabilityDescriptor(
        name="command", origin="test", risk="execute", effect_kind=EffectKind.EXECUTION,
        requires_approval=True, parameters={"type": "object", "properties": {}}, timeout_seconds=1,
    ), execute)
    result = await manager.capability_gateway.invoke(CapabilityInvocation(
        execution_id="run", provider_call_id="call", name="command",
    ))
    assert result.status is CapabilityStatus.DENIED
    assert manager.task_workspace.state_for(session_id).effects == ()
    assert manager.task_workspace.completion_blockers(session_id) == []
