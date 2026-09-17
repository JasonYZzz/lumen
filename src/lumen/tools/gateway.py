"""Provider-neutral capability catalog and execution seam for alternate transports.

Realtime provider tool-call objects stop here.  The gateway projects the same
ToolSpec, permission policy and effect recorder used by the text runtime, so a
transport adapter cannot invent a second capability policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Tool
from pydantic_ai.tools import RunContext

from lumen.events import ApprovalRequest
from lumen.tools.presentation import ToolPresentationCatalog
from lumen.tools.registry import PermissionDecision, PermissionPolicy, RegisteredTool, ToolRegistry
from lumen.tools.spec import EffectKind, ToolConcurrency, ToolOutputSpec, ToolSpec


class CapabilityStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    REPLAYED = "replayed"


class ToolGuardDecision(StrEnum):
    ABSTAIN = "abstain"
    DENY = "deny"


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    parameters: dict[str, Any]
    origin: str
    risk: str
    effect_kind: EffectKind
    concurrency: ToolConcurrency = ToolConcurrency.EXCLUSIVE
    requires_approval: bool = False
    timeout_seconds: float = Field(gt=0)
    deferred: bool = False


class CapabilityInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    provider_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class ToolExecutionIdentity:
    """Frozen facts supplied to monotonic, fail-closed tool guards."""

    execution_id: str
    provider_call_id: str
    name: str
    arguments: Mapping[str, Any]
    origin: str
    risk: str
    effect_kind: EffectKind


class CapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation: CapabilityInvocation
    status: CapabilityStatus
    output: Any = None
    model_output: str | None = None
    presentation: dict[str, Any] | None = None
    call_view: dict[str, Any] | None = None
    result_view: dict[str, Any] | None = None
    error: str | None = None
    effect_receipt_id: str | None = None
    idempotency_key: str
    # None means no executor ran (denial, validation failure or replay).
    execution_seconds: float | None = Field(default=None, ge=0)

    @property
    def succeeded(self) -> bool:
        return self.status in {CapabilityStatus.SUCCEEDED, CapabilityStatus.REPLAYED}


@dataclass(frozen=True, slots=True)
class CapabilityApproval:
    approved: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class CapabilityBeforeDecision:
    allow: bool = True
    arguments: dict[str, Any] | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PreparedCapability:
    invocation: CapabilityInvocation
    denial_message: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityReplay:
    """Canonical raw output restored from an explicit recovery receipt."""

    output: object


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[CapabilityApproval]]
CapabilityBeforeHandler = Callable[[CapabilityInvocation], Awaitable[CapabilityBeforeDecision]]
CapabilityAfterHandler = Callable[[CapabilityInvocation, CapabilityResult], Awaitable[str | None]]
CapabilityReplayHandler = Callable[[CapabilityInvocation], CapabilityReplay | None]
CapabilitySuccessHandler = Callable[[CapabilityInvocation, object], None]
EffectRecorder = Callable[..., object]
ToolGuard = Callable[[ToolExecutionIdentity], ToolGuardDecision]


@dataclass(slots=True)
class _LocalCapability:
    entry: RegisteredTool
    descriptor: CapabilityDescriptor
    tool: Tool[None]


CapabilityExecutor = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class _ExternalCapability:
    """Adapter-backed capability, for example a connected MCP tool."""

    descriptor: CapabilityDescriptor
    execute: CapabilityExecutor


def _safe_json(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return MappingProxyType({key: _freeze_json(item) for key, item in mapping.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in cast(list[Any], value))
    return value


def _idempotency_key(invocation: CapabilityInvocation) -> str:
    encoded = json.dumps(
        invocation.arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(
        f"{invocation.execution_id}\n{invocation.provider_call_id}\n{invocation.name}\n{encoded}".encode()
    ).hexdigest()
    return f"sha256:{digest}"


class CapabilityGateway:
    """Catalog and invoke local capabilities with one auditable contract."""

    _SELF_RECORDING = frozenset(
        {
            "write_file",
            "edit_file",
            "download_file",
            "install_skill",
            "open_work_product",
            "inspect_work_product",
            "change_work_product",
            "restore_work_product",
        }
    )

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        *,
        default_timeout: float,
        effect_recorder: EffectRecorder | None = None,
        guards: Sequence[ToolGuard] = (),
        before_invoke: CapabilityBeforeHandler | None = None,
        after_invoke: CapabilityAfterHandler | None = None,
        replay: CapabilityReplayHandler | None = None,
        record_success: CapabilitySuccessHandler | None = None,
    ) -> None:
        self._capabilities: dict[str, _LocalCapability | _ExternalCapability] = {}
        self._policy = policy
        self._default_timeout = default_timeout
        self._effect_recorder = effect_recorder
        self._guards = tuple(guards)
        self._before_invoke = before_invoke
        self._after_invoke = after_invoke
        self._replay = replay
        self._record_success = record_success
        self._results: dict[str, CapabilityResult] = {}
        self._presentation_specs = {name: entry.spec for name, entry in registry.entries.items()}
        for name, entry in registry.entries.items():
            self._add_local(name, entry)
        self._presenter = ToolPresentationCatalog(self._presentation_specs)

    def _add_local(self, name: str, entry: RegisteredTool) -> None:
        decision = self._policy.decide(name, entry.spec.risk)
        if decision is PermissionDecision.DENY:
            return
        tool: Tool[None] = Tool(
            entry.spec.function,
            name=name,
            description=entry.spec.description,
            sequential=True,
            requires_approval=decision is PermissionDecision.CONFIRM,
            timeout=entry.spec.timeout or self._default_timeout,
        )
        schema = tool.function_schema
        try:
            concurrency = entry.spec.concurrency_for({})
        except (KeyError, TypeError, ValueError):
            concurrency = ToolConcurrency.EXCLUSIVE
        descriptor = CapabilityDescriptor(
            name=name,
            description=tool.description or schema.description or "",
            parameters=schema.json_schema,
            origin=entry.origin,
            risk=entry.spec.risk.value,
            effect_kind=entry.spec.effect,
            concurrency=concurrency,
            requires_approval=decision is PermissionDecision.CONFIRM,
            timeout_seconds=entry.spec.timeout or self._default_timeout,
        )
        self._capabilities[name] = _LocalCapability(entry, descriptor, tool)

    def derive(
        self,
        specs: Sequence[tuple[ToolSpec, str]],
        *,
        replay: CapabilityReplayHandler | None = None,
        record_success: CapabilitySuccessHandler | None = None,
    ) -> CapabilityGateway:
        """Create a run-local catalog overlay without a second policy authority."""

        derived = CapabilityGateway.__new__(CapabilityGateway)
        derived._capabilities = dict(self._capabilities)
        derived._policy = self._policy
        derived._default_timeout = self._default_timeout
        derived._effect_recorder = self._effect_recorder
        derived._guards = self._guards
        derived._before_invoke = self._before_invoke
        derived._after_invoke = self._after_invoke
        derived._replay = replay if replay is not None else self._replay
        derived._record_success = (
            record_success if record_success is not None else self._record_success
        )
        derived._results = {}
        derived._presentation_specs = dict(self._presentation_specs)
        for spec, origin in specs:
            name = spec.name
            if name is None:
                raise ValueError("derived capability has no name")
            if name in derived._capabilities:
                raise ValueError(f"capability already registered: {name}")
            entry = RegisteredTool(spec, origin)
            derived._presentation_specs[name] = spec
            derived._add_local(name, entry)
        derived._presenter = ToolPresentationCatalog(derived._presentation_specs)
        return derived

    def narrow(
        self,
        names: Sequence[str],
        *,
        replacements: Sequence[tuple[ToolSpec, str]] = (),
        effect_recorder: EffectRecorder | None = None,
    ) -> CapabilityGateway:
        """Create an isolated child catalog whose authority can only shrink.

        External capabilities keep their parent-owned connection Adapter.
        Replacement local specs are used for workspace-bound tools that must
        be rebound to a child worktree. Results and idempotency state never
        leak between parent and child executions.
        """

        allowed = frozenset(names)
        replacement_names = {
            name for spec, _origin in replacements if (name := spec.name) is not None
        }
        unknown_replacements = replacement_names - allowed
        if unknown_replacements:
            raise ValueError(
                f"child capability replacements exceed the allowed set: {sorted(unknown_replacements)}"
            )
        narrowed = CapabilityGateway.__new__(CapabilityGateway)
        narrowed._capabilities = {
            name: capability
            for name, capability in self._capabilities.items()
            if name in allowed and name not in replacement_names
        }
        narrowed._policy = self._policy
        narrowed._default_timeout = self._default_timeout
        narrowed._effect_recorder = effect_recorder
        narrowed._guards = self._guards
        narrowed._before_invoke = self._before_invoke
        narrowed._after_invoke = self._after_invoke
        narrowed._replay = None
        narrowed._record_success = None
        narrowed._results = {}
        narrowed._presentation_specs = {
            name: spec
            for name, spec in self._presentation_specs.items()
            if name in allowed and name not in replacement_names
        }
        for spec, origin in replacements:
            name = spec.name
            if name is None:
                raise ValueError("child capability replacement has no name")
            entry = RegisteredTool(spec, origin)
            narrowed._presentation_specs[name] = spec
            narrowed._add_local(name, entry)
        narrowed._presenter = ToolPresentationCatalog(narrowed._presentation_specs)
        return narrowed

    def register(
        self,
        descriptor: CapabilityDescriptor,
        execute: CapabilityExecutor,
    ) -> Callable[[], None]:
        """Attach a connected provider-neutral Adapter to the catalog.

        Registration is intentionally explicit and happens only after the
        outer resource lifecycle has opened the remote service and validated
        its schema.  This keeps connection ownership outside the gateway.
        """

        if descriptor.name in self._capabilities:
            raise ValueError(f"capability already registered: {descriptor.name}")
        capability = _ExternalCapability(descriptor, execute)
        self._capabilities[descriptor.name] = capability

        def dispose() -> None:
            if self._capabilities.get(descriptor.name) is capability:
                del self._capabilities[descriptor.name]

        return dispose

    def catalog(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.descriptor for _, item in sorted(self._capabilities.items()))

    def descriptor(self, name: str) -> CapabilityDescriptor | None:
        capability = self._capabilities.get(name)
        return capability.descriptor if capability is not None else None

    def concurrency_for(self, invocation: CapabilityInvocation) -> ToolConcurrency:
        """Resolve scheduling for this invocation, never from catalog-time placeholders."""

        capability = self._capabilities.get(invocation.name)
        if capability is None:
            return ToolConcurrency.EXCLUSIVE
        if isinstance(capability, _ExternalCapability):
            return capability.descriptor.concurrency
        try:
            return capability.entry.spec.concurrency_for(invocation.arguments)
        except (KeyError, TypeError, ValueError):
            return ToolConcurrency.EXCLUSIVE

    def approval_request(self, invocation: CapabilityInvocation) -> ApprovalRequest | None:
        """Return the gateway-owned approval projection for one invocation."""

        capability = self._capabilities.get(invocation.name)
        if capability is None or not capability.descriptor.requires_approval:
            return None
        return ApprovalRequest(
            call_id=invocation.provider_call_id,
            name=invocation.name,
            args=invocation.arguments,
            origin=capability.descriptor.origin,
            risk=capability.descriptor.risk,
        )

    async def prepare(self, invocation: CapabilityInvocation) -> PreparedCapability:
        """Apply hooks and freeze the validated invocation used by every policy stage."""

        capability = self._capabilities.get(invocation.name)
        if capability is None:
            return PreparedCapability(invocation)
        effective = invocation
        if self._before_invoke is not None:
            try:
                decision = await self._before_invoke(invocation)
            except Exception as error:
                return PreparedCapability(
                    invocation,
                    f"pre-invoke pipeline failed closed: {type(error).__name__}: {error}",
                )
            effective = (
                invocation.model_copy(update={"arguments": decision.arguments})
                if decision.arguments is not None
                else invocation
            )
            if not decision.allow:
                return PreparedCapability(
                    effective,
                    decision.reason or "capability denied by pre-invoke hook",
                )
        if isinstance(capability, _LocalCapability):
            try:
                raw_validated = capability.tool.function_schema.validator.validate_python(
                    effective.arguments
                )
                if not isinstance(raw_validated, dict):
                    raise TypeError("capability arguments did not validate to an object")
                effective = effective.model_copy(
                    update={"arguments": cast(dict[str, Any], raw_validated)}
                )
            except (ValidationError, ValueError, TypeError) as error:
                return PreparedCapability(
                    effective,
                    failure_message=f"{type(error).__name__}: {error}",
                )
        return PreparedCapability(effective)

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        approve: ApprovalHandler | None = None,
    ) -> CapabilityResult:
        return await self.invoke_prepared(await self.prepare(invocation), approve=approve)

    async def invoke_prepared(
        self,
        prepared: PreparedCapability,
        *,
        approve: ApprovalHandler | None = None,
    ) -> CapabilityResult:
        invocation = prepared.invocation
        key = _idempotency_key(invocation)
        prior = self._results.get(key)
        if prior is not None:
            if prior.status in {CapabilityStatus.SUCCEEDED, CapabilityStatus.REPLAYED}:
                return prior.model_copy(update={
                    "status": CapabilityStatus.REPLAYED, "execution_seconds": None,
                })
            return prior.model_copy(update={"execution_seconds": None})

        if prepared.denial_message is not None:
            return self._store(
                key,
                CapabilityResult(
                    invocation=invocation,
                    status=CapabilityStatus.DENIED,
                    error=prepared.denial_message,
                    idempotency_key=key,
                ),
            )

        capability = self._capabilities.get(invocation.name)
        if capability is None:
            return self._store(
                key,
                CapabilityResult(
                    invocation=invocation,
                    status=CapabilityStatus.FAILED,
                    error=f"unknown or denied capability: {invocation.name}",
                    idempotency_key=key,
                ),
            )

        if prepared.failure_message is not None:
            return self._store(
                key,
                CapabilityResult(
                    invocation=invocation,
                    status=CapabilityStatus.FAILED,
                    error=prepared.failure_message,
                    idempotency_key=key,
                ),
            )

        if capability.descriptor.requires_approval:
            if approve is None:
                return self._store(
                    key,
                    CapabilityResult(
                        invocation=invocation,
                        status=CapabilityStatus.DENIED,
                        error="approval is required but no approval handler is available",
                        idempotency_key=key,
                    ),
                )
            decision = await approve(
                ApprovalRequest(
                    call_id=invocation.provider_call_id,
                    name=invocation.name,
                    args=invocation.arguments,
                    origin=capability.descriptor.origin,
                    risk=capability.descriptor.risk,
                )
            )
            if not decision.approved:
                return self._store(
                    key,
                    CapabilityResult(
                        invocation=invocation,
                        status=CapabilityStatus.DENIED,
                        error=decision.message or "The user denied this capability call.",
                        idempotency_key=key,
                    ),
                )

        identity = ToolExecutionIdentity(
            execution_id=invocation.execution_id,
            provider_call_id=invocation.provider_call_id,
            name=invocation.name,
            arguments=cast(Mapping[str, Any], _freeze_json(_safe_json(invocation.arguments))),
            origin=capability.descriptor.origin,
            risk=capability.descriptor.risk,
            effect_kind=capability.descriptor.effect_kind,
        )
        try:
            denied = any(guard(identity) is ToolGuardDecision.DENY for guard in self._guards)
        except Exception as error:
            return self._store(
                key,
                CapabilityResult(
                    invocation=invocation,
                    status=CapabilityStatus.DENIED,
                    error=f"tool guard failed closed: {type(error).__name__}: {error}",
                    idempotency_key=key,
                ),
            )
        if denied:
            return self._store(
                key,
                CapabilityResult(
                    invocation=invocation,
                    status=CapabilityStatus.DENIED,
                    error="tool invocation denied by monotonic guard",
                    idempotency_key=key,
                ),
            )

        receipt_id: str | None = None
        execution_seconds: float | None = None
        try:
            contract = (
                capability.entry.spec.output_contract
                if isinstance(capability, _LocalCapability)
                else ToolOutputSpec()
            )
            replayed = self._replay(invocation) if self._replay is not None else None
            if replayed is not None:
                canonical = contract.validate(replayed.output)
                return await self._finish_success(
                    key,
                    invocation,
                    capability,
                    contract,
                    canonical,
                    status=CapabilityStatus.REPLAYED,
                    record_effect=False,
                    record_recovery=False,
                )
            if capability.descriptor.effect_kind in {
                EffectKind.UNKNOWN, EffectKind.EXTERNAL_ACTION, EffectKind.MUTATION, EffectKind.EXECUTION,
            }:
                receipt_id = self._record_effect(capability, invocation.name, None, "prepared")
            execution_started = time.monotonic()
            try:
                if isinstance(capability, _LocalCapability):
                    context = cast(RunContext[None], None)
                    pending = capability.tool.function_schema.call(invocation.arguments, context)
                else:
                    pending = capability.execute(invocation.arguments)
                raw_result = await asyncio.wait_for(
                    pending,
                    timeout=capability.descriptor.timeout_seconds,
                )
            finally:
                execution_seconds = time.monotonic() - execution_started
            canonical = contract.validate(raw_result)
        except asyncio.CancelledError as error:
            self._finish_failure(
                key, invocation, capability, error, receipt_id=receipt_id,
                execution_seconds=execution_seconds,
            )
            raise
        except Exception as error:  # provider-facing calls must receive a typed failure
            return self._finish_failure(
                key, invocation, capability, error, receipt_id=receipt_id,
                execution_seconds=execution_seconds,
            )

        return await self._finish_success(
            key,
            invocation,
            capability,
            contract,
            canonical,
            status=CapabilityStatus.SUCCEEDED,
            record_effect=True,
            record_recovery=True,
            receipt_id=receipt_id,
            execution_seconds=execution_seconds,
        )

    async def _finish_success(
        self,
        key: str,
        invocation: CapabilityInvocation,
        capability: _LocalCapability | _ExternalCapability,
        contract: ToolOutputSpec,
        canonical: object,
        *,
        status: CapabilityStatus,
        record_effect: bool,
        record_recovery: bool,
        receipt_id: str | None = None,
        execution_seconds: float | None = None,
    ) -> CapabilityResult:
        model_output = contract.model_text(canonical)
        # Client presentation is a fallible projection, never part of the
        # authoritative execution outcome exposed to the model/effect journal.
        try:
            presentation = contract.presentation(canonical)
        except Exception:
            presentation = None

        if record_recovery and self._record_success is not None:
            self._record_success(invocation, canonical)
        receipt_id = (
            self._record_effect(capability, invocation.name, True, "succeeded", receipt_id=receipt_id)
            if record_effect
            else None
        )
        call_view = self._presenter.call_view(
            invocation.name,
            invocation.arguments,
            origin=capability.descriptor.origin,
            risk=capability.descriptor.risk,
        )
        result_view = self._presenter.result_view(
            invocation.name,
            invocation.arguments,
            model_output,
            is_error=False,
        )
        result = CapabilityResult(
            invocation=invocation,
            status=status,
            output=canonical,
            model_output=model_output,
            presentation=presentation,
            call_view=call_view.model_dump(mode="json"),
            result_view=result_view.model_dump(mode="json"),
            effect_receipt_id=receipt_id,
            idempotency_key=key,
            execution_seconds=execution_seconds,
        )
        if self._after_invoke is not None:
            try:
                modified_output = await self._after_invoke(invocation, result)
            except Exception:
                modified_output = None
            if modified_output is not None:
                result = result.model_copy(update={"model_output": modified_output})
        return self._store(key, result)

    def _finish_failure(
        self,
        key: str,
        invocation: CapabilityInvocation,
        capability: _LocalCapability | _ExternalCapability,
        error: BaseException,
        *,
        receipt_id: str | None = None,
        execution_seconds: float | None = None,
    ) -> CapabilityResult:
        message = f"{type(error).__name__}: {error}"
        receipt_id = self._record_effect(
            capability, invocation.name, False, message, receipt_id=receipt_id,
        )
        call_view = self._presenter.call_view(
            invocation.name,
            invocation.arguments,
            origin=capability.descriptor.origin,
            risk=capability.descriptor.risk,
        )
        result_view = self._presenter.result_view(
            invocation.name,
            invocation.arguments,
            message,
            is_error=True,
        )
        return self._store(
            key,
            CapabilityResult(
                invocation=invocation,
                status=CapabilityStatus.FAILED,
                error=message,
                call_view=call_view.model_dump(mode="json"),
                result_view=result_view.model_dump(mode="json"),
                effect_receipt_id=receipt_id,
                idempotency_key=key,
                execution_seconds=execution_seconds,
            ),
        )

    def _record_effect(
        self,
        capability: _LocalCapability | _ExternalCapability,
        name: str,
        success: bool | None,
        summary: str,
        *,
        receipt_id: str | None = None,
    ) -> str | None:
        if (
            self._effect_recorder is None
            or name in self._SELF_RECORDING
            or capability.descriptor.effect_kind is EffectKind.OBSERVE
        ):
            return None
        kwargs: dict[str, object] = {"receipt_id": receipt_id} if receipt_id is not None else {}
        receipt = self._effect_recorder(
            tool_name=name,
            effect_kind=capability.descriptor.effect_kind,
            success=success,
            summary=f"{name} {summary}",
            **kwargs,
        )
        value = getattr(receipt, "id", None)
        return str(value) if value is not None else None

    def _store(self, key: str, result: CapabilityResult) -> CapabilityResult:
        self._results[key] = result
        return result


__all__ = [
    "CapabilityAfterHandler",
    "CapabilityApproval",
    "CapabilityBeforeDecision",
    "CapabilityBeforeHandler",
    "CapabilityDescriptor",
    "CapabilityExecutor",
    "CapabilityGateway",
    "CapabilityInvocation",
    "CapabilityReplay",
    "CapabilityReplayHandler",
    "CapabilityResult",
    "CapabilityStatus",
    "CapabilitySuccessHandler",
    "PreparedCapability",
    "ToolExecutionIdentity",
    "ToolGuard",
    "ToolGuardDecision",
]
