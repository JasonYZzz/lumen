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


@pytest.mark.parametrize("ending", ["success", "timeout", "cancel"])
async def test_external_receipt_is_durable_before_dispatch_and_reconciles_uncertainty(
    tmp_path: Path, ending: str,
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
        if ending != "success":
            await asyncio.Event().wait()
        return "accepted"

    manager.capability_gateway.register(CapabilityDescriptor(
        name="remote_update", origin="mcp:remote", risk="write", effect_kind=EffectKind.EXTERNAL_ACTION,
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
