"""Provider-neutral capability catalog and execution seam for alternate transports.

Realtime provider tool-call objects stop here.  The gateway projects the same
ToolSpec, permission policy and effect recorder used by the text runtime, so a
transport adapter cannot invent a second capability policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Tool
from pydantic_ai.tools import RunContext

from lumen.events import ApprovalRequest
from lumen.tools.registry import PermissionDecision, PermissionPolicy, RegisteredTool, ToolRegistry
from lumen.tools.spec import EffectKind


class CapabilityStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    REPLAYED = "replayed"


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""
    parameters: dict[str, Any]
    origin: str
    risk: str
    effect_kind: EffectKind
    requires_approval: bool = False
    timeout_seconds: float = Field(gt=0)


class CapabilityInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str
    provider_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict[str, Any])


class CapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation: CapabilityInvocation
    status: CapabilityStatus
    output: Any = None
    error: str | None = None
    effect_receipt_id: str | None = None
    idempotency_key: str

    @property
    def succeeded(self) -> bool:
        return self.status in {CapabilityStatus.SUCCEEDED, CapabilityStatus.REPLAYED}


@dataclass(frozen=True, slots=True)
class CapabilityApproval:
    approved: bool
    message: str = ""


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[CapabilityApproval]]
EffectRecorder = Callable[..., object]


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
    ) -> None:
        self._capabilities: dict[str, _LocalCapability | _ExternalCapability] = {}
        self._effect_recorder = effect_recorder
        self._results: dict[str, CapabilityResult] = {}
        for name, entry in registry.entries.items():
            decision = policy.decide(name, entry.spec.risk)
            if decision is PermissionDecision.DENY:
                continue
            tool: Tool[None] = Tool(
                entry.spec.function,
                name=name,
                description=entry.spec.description,
                sequential=True,
                requires_approval=decision is PermissionDecision.CONFIRM,
                timeout=entry.spec.timeout or default_timeout,
            )
            schema = tool.function_schema
            descriptor = CapabilityDescriptor(
                name=name,
                description=tool.description or schema.description or "",
                parameters=schema.json_schema,
                origin=entry.origin,
                risk=entry.spec.risk.value,
                effect_kind=entry.spec.effect,
                requires_approval=decision is PermissionDecision.CONFIRM,
                timeout_seconds=entry.spec.timeout or default_timeout,
            )
            self._capabilities[name] = _LocalCapability(entry, descriptor, tool)

    def register(
        self,
        descriptor: CapabilityDescriptor,
        execute: CapabilityExecutor,
    ) -> None:
        """Attach a connected provider-neutral Adapter to the catalog.

        Registration is intentionally explicit and happens only after the
        outer resource lifecycle has opened the remote service and validated
        its schema.  This keeps connection ownership outside the gateway.
        """

        if descriptor.name in self._capabilities:
            raise ValueError(f"capability already registered: {descriptor.name}")
        self._capabilities[descriptor.name] = _ExternalCapability(descriptor, execute)

    def catalog(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.descriptor for _, item in sorted(self._capabilities.items()))

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        approve: ApprovalHandler | None = None,
    ) -> CapabilityResult:
        key = _idempotency_key(invocation)
        prior = self._results.get(key)
        if prior is not None:
            return prior.model_copy(update={"status": CapabilityStatus.REPLAYED})

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

        try:
            if isinstance(capability, _LocalCapability):
                raw_validated = capability.tool.function_schema.validator.validate_python(
                    invocation.arguments
                )
                if not isinstance(raw_validated, dict):
                    raise TypeError("capability arguments did not validate to an object")
                validated = cast(dict[str, Any], raw_validated)
                context = cast(RunContext[None], None)
                pending = capability.tool.function_schema.call(validated, context)
            else:
                pending = capability.execute(invocation.arguments)
            result = await asyncio.wait_for(
                pending,
                timeout=capability.descriptor.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (ValidationError, ValueError, TypeError, TimeoutError) as error:
            return self._finish_failure(key, invocation, capability, error)
        except Exception as error:  # provider-facing calls must receive a typed failure
            return self._finish_failure(key, invocation, capability, error)

        receipt_id = self._record_effect(capability, invocation.name, True, "succeeded")
        return self._store(
            key,
            CapabilityResult(
                invocation=invocation,
                status=CapabilityStatus.SUCCEEDED,
                output=_safe_json(result),
                effect_receipt_id=receipt_id,
                idempotency_key=key,
            ),
        )

    def _finish_failure(
        self,
        key: str,
        invocation: CapabilityInvocation,
        capability: _LocalCapability | _ExternalCapability,
        error: BaseException,
    ) -> CapabilityResult:
        message = f"{type(error).__name__}: {error}"
        receipt_id = self._record_effect(capability, invocation.name, False, message)
        return self._store(
            key,
            CapabilityResult(
                invocation=invocation,
                status=CapabilityStatus.FAILED,
                error=message,
                effect_receipt_id=receipt_id,
                idempotency_key=key,
            ),
        )

    def _record_effect(
        self,
        capability: _LocalCapability | _ExternalCapability,
        name: str,
        success: bool,
        summary: str,
    ) -> str | None:
        if (
            self._effect_recorder is None
            or name in self._SELF_RECORDING
            or capability.descriptor.effect_kind is EffectKind.OBSERVE
        ):
            return None
        receipt = self._effect_recorder(
            tool_name=name,
            effect_kind=capability.descriptor.effect_kind,
            success=success,
            summary=f"{name} {summary}",
        )
        value = getattr(receipt, "id", None)
        return str(value) if value is not None else None

    def _store(self, key: str, result: CapabilityResult) -> CapabilityResult:
        self._results[key] = result
        return result


__all__ = [
    "CapabilityApproval",
    "CapabilityDescriptor",
    "CapabilityExecutor",
    "CapabilityGateway",
    "CapabilityInvocation",
    "CapabilityResult",
    "CapabilityStatus",
]
