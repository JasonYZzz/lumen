from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumen.config import PermissionsConfig
from lumen.events import ApprovalRequest
from lumen.tools.gateway import (
    CapabilityApproval,
    CapabilityBeforeDecision,
    CapabilityGateway,
    CapabilityInvocation,
    CapabilityReplay,
    CapabilityResult,
    CapabilityStatus,
    ToolExecutionIdentity,
    ToolGuardDecision,
)
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency, ToolSpec


def _gateway(tmp_path: Path, *specs: ToolSpec) -> CapabilityGateway:
    registry = ToolRegistry(tmp_path)
    registry.add_many(list(specs), origin="test")
    return CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=0.1,
    )


def _invocation(name: str, arguments: dict[str, object], *, call_id: str = "call-1") -> CapabilityInvocation:
    return CapabilityInvocation(
        execution_id="execution-1",
        provider_call_id=call_id,
        name=name,
        arguments=arguments,
    )


def test_gateway_resolves_argument_sensitive_concurrency_at_invocation_time(tmp_path: Path) -> None:
    async def inspect_path(path: str, safe: bool) -> str:
        return f"{path}:{safe}"

    gateway = _gateway(
        tmp_path,
        ToolSpec(
            inspect_path,
            risk=Risk.READ,
            concurrency=lambda arguments: (
                ToolConcurrency.PARALLEL_SAFE if arguments["safe"] is True else ToolConcurrency.EXCLUSIVE
            ),
        ),
    )

    assert (
        gateway.concurrency_for(_invocation("inspect_path", {"path": "a", "safe": True}))
        is ToolConcurrency.PARALLEL_SAFE
    )
    assert (
        gateway.concurrency_for(_invocation("inspect_path", {"path": "a", "safe": False}))
        is ToolConcurrency.EXCLUSIVE
    )
    assert gateway.concurrency_for(_invocation("inspect_path", {"path": "a"})) is ToolConcurrency.EXCLUSIVE


async def test_gateway_validates_arguments_and_replays_only_success(tmp_path: Path) -> None:
    calls = 0

    async def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    gateway = _gateway(tmp_path, ToolSpec(double, risk=Risk.READ))
    invocation = _invocation("double", {"value": 3})

    first = await gateway.invoke(invocation)
    replay = await gateway.invoke(invocation)
    malformed = await gateway.invoke(_invocation("double", {"value": "not-an-integer"}, call_id="call-2"))

    assert first.status is CapabilityStatus.SUCCEEDED
    assert first.output == 6
    assert replay.status is CapabilityStatus.REPLAYED
    assert replay.output == 6
    assert malformed.status is CapabilityStatus.FAILED
    assert malformed.error is not None
    assert calls == 1


async def test_gateway_denial_is_stable_and_unknown_tools_fail_closed(tmp_path: Path) -> None:
    calls = 0

    async def mutate(value: str) -> str:
        nonlocal calls
        calls += 1
        return value

    gateway = _gateway(
        tmp_path,
        ToolSpec(mutate, risk=Risk.WRITE, effect_kind=EffectKind.MUTATION),
    )
    invocation = _invocation("mutate", {"value": "x"})

    denied = await gateway.invoke(invocation)

    async def approve(_request: object) -> CapabilityApproval:
        return CapabilityApproval(True, "approved too late")

    replayed_denial = await gateway.invoke(invocation, approve=approve)
    unknown = await gateway.invoke(_invocation("missing", {}, call_id="call-unknown"))

    assert denied.status is CapabilityStatus.DENIED
    assert replayed_denial.status is CapabilityStatus.DENIED
    assert unknown.status is CapabilityStatus.FAILED
    assert "unknown or denied" in (unknown.error or "")
    assert calls == 0


async def test_gateway_timeout_is_a_typed_failure(tmp_path: Path) -> None:
    async def slow() -> str:
        await asyncio.sleep(0.1)
        return "late"

    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(slow, risk=Risk.READ, timeout=0.001), origin="test")
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )

    result = await gateway.invoke(_invocation("slow", {}))

    assert result.status is CapabilityStatus.FAILED
    assert result.error is not None
    assert "TimeoutError" in result.error
    assert result.execution_seconds is not None


async def test_executor_timing_excludes_approval_hooks_and_replayed_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_value = 0.0

    async def before(_invocation: CapabilityInvocation) -> CapabilityBeforeDecision:
        nonlocal clock_value
        clock_value += 7
        return CapabilityBeforeDecision()

    async def approve(_request: ApprovalRequest) -> CapabilityApproval:
        nonlocal clock_value
        clock_value += 11
        return CapabilityApproval(True)

    async def mutate() -> str:
        nonlocal clock_value
        clock_value += 5
        return "written"

    async def after(_invocation: CapabilityInvocation, _result: CapabilityResult) -> None:
        nonlocal clock_value
        clock_value += 13

    monkeypatch.setattr("lumen.tools.gateway.time", SimpleNamespace(monotonic=lambda: clock_value))
    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(mutate, risk=Risk.WRITE), origin="test")
    gateway = CapabilityGateway(registry, PermissionPolicy(PermissionsConfig()), default_timeout=1,
                                before_invoke=before, after_invoke=after)
    invocation = _invocation("mutate", {})
    result = await gateway.invoke(invocation, approve=approve)
    assert result.succeeded
    assert result.execution_seconds == 5
    assert clock_value == 36
    replay = await gateway.invoke(invocation, approve=approve)
    assert replay.status is CapabilityStatus.REPLAYED
    assert replay.execution_seconds is None
    assert clock_value == 43  # Only the prepare hook runs on this replay.


async def test_gateway_hooks_run_before_approval_and_after_canonical_result(tmp_path: Path) -> None:
    received: list[str] = []
    approved_args: list[dict[str, object]] = []

    async def mutate(value: str) -> str:
        received.append(value)
        return value

    async def before(invocation: CapabilityInvocation) -> CapabilityBeforeDecision:
        return CapabilityBeforeDecision(arguments={"value": str(invocation.arguments["value"]).upper()})

    async def after(_invocation: CapabilityInvocation, result: CapabilityResult) -> str:
        return f"hooked:{result.model_output or ''}"

    async def approve(request: ApprovalRequest) -> CapabilityApproval:
        approved_args.append(dict(request.args))
        return CapabilityApproval(True, "approved")

    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(mutate, risk=Risk.WRITE), origin="test")
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
        before_invoke=before,
        after_invoke=after,
    )

    result = await gateway.invoke(
        _invocation("mutate", {"value": "hello"}),
        approve=approve,
    )

    assert result.status is CapabilityStatus.SUCCEEDED
    assert result.output == "HELLO"
    assert result.model_output == "hooked:HELLO"
    assert approved_args == [{"value": "HELLO"}]
    assert received == ["HELLO"]


async def test_gateway_pre_hook_denial_never_executes_or_requests_approval(tmp_path: Path) -> None:
    executed = False

    async def mutate() -> str:
        nonlocal executed
        executed = True
        return "changed"

    async def deny(_invocation: CapabilityInvocation) -> CapabilityBeforeDecision:
        return CapabilityBeforeDecision(False, reason="blocked by hook")

    async def approve(_request: ApprovalRequest) -> CapabilityApproval:
        raise AssertionError("hook-denied capability must not request approval")

    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(mutate, risk=Risk.WRITE), origin="test")
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
        before_invoke=deny,
    )

    result = await gateway.invoke(_invocation("mutate", {}), approve=approve)

    assert result.status is CapabilityStatus.DENIED
    assert result.error == "blocked by hook"
    assert executed is False


async def test_derived_gateway_replays_validated_output_without_executing_effect(
    tmp_path: Path,
) -> None:
    calls = 0
    recorded: list[tuple[CapabilityInvocation, object]] = []

    async def mutate(value: int) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": value}

    approved_args: list[dict[str, object]] = []

    async def approve(request: ApprovalRequest) -> CapabilityApproval:
        approved_args.append(dict(request.args))
        return CapabilityApproval(True, "approved")

    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(mutate, risk=Risk.WRITE, effect_kind=EffectKind.MUTATION), origin="test")
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    ).derive(
        (),
        replay=lambda invocation: (
            CapabilityReplay({"value": invocation.arguments["value"]})
            if invocation.name == "mutate"
            else None
        ),
        record_success=lambda invocation, output: recorded.append((invocation, output)),
    )

    result = await gateway.invoke(_invocation("mutate", {"value": "7"}), approve=approve)

    assert result.status is CapabilityStatus.REPLAYED
    assert result.invocation.arguments == {"value": 7}
    assert approved_args == [{"value": 7}]
    assert result.output == {"value": 7}
    assert calls == 0
    assert recorded == []


async def test_gateway_uses_one_validated_identity_for_approval_guard_and_execution(
    tmp_path: Path,
) -> None:
    approved: list[dict[str, object]] = []
    guarded: list[dict[str, object]] = []
    executed: list[int] = []

    async def mutate(value: int) -> int:
        executed.append(value)
        return value

    async def approve(request: ApprovalRequest) -> CapabilityApproval:
        approved.append(dict(request.args))
        return CapabilityApproval(True)

    def guard(identity: ToolExecutionIdentity) -> ToolGuardDecision:
        guarded.append(dict(identity.arguments))
        return ToolGuardDecision.ABSTAIN

    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(mutate, risk=Risk.WRITE), origin="test")
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
        guards=(guard,),
    )

    result = await gateway.invoke(_invocation("mutate", {"value": "7"}), approve=approve)

    assert result.invocation.arguments == {"value": 7}
    assert approved == guarded == [{"value": 7}]
    assert executed == [7]
