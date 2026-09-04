"""Explicit single-agent model/tool loop owned by Lumen.

Tool calls cross the existing CapabilityGateway Seam for validation, approval,
scheduling, execution, effect receipts and idempotency. The loop never invokes
ToolSpec functions directly and ModelDriver remains transport-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import random
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Annotated, Any, Generic, Literal, NoReturn, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from lumen.agent_loop.driver import (
    ModelDriver,
    ModelDriverRequest,
    ModelProviderError,
    ModelResponseStarted,
    ModelStopReason,
    ModelStreamEvent,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolArgumentsDelta,
    ModelToolCallCompleted,
    ModelToolCallStarted,
    ModelUsage,
)
from lumen.completion import CompletionBlocker
from lumen.events import ApprovalRequest
from lumen.tools.gateway import (
    ApprovalHandler as GatewayApprovalHandler,
)
from lumen.tools.gateway import (
    CapabilityApproval,
    CapabilityGateway,
    CapabilityInvocation,
    CapabilityResult,
    PreparedCapability,
)
from lumen.tools.spec import ToolConcurrency

MessageT = TypeVar("MessageT")


class _LoopContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LoopState(StrEnum):
    PREPARING = "preparing"
    REQUESTING_MODEL = "requesting_model"
    STREAMING_MODEL = "streaming_model"
    COLLECTING_TOOL_CALLS = "collecting_tool_calls"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING_TOOLS = "executing_tools"
    APPENDING_TOOL_RESULTS = "appending_tool_results"
    VALIDATING_COMPLETION = "validating_completion"
    WAITING_FOR_USER = "waiting_for_user"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_TERMINAL_STATES = frozenset({LoopState.COMPLETED, LoopState.CANCELLED, LoopState.FAILED})
_ALLOWED_TRANSITIONS: Mapping[LoopState, frozenset[LoopState]] = {
    LoopState.PREPARING: frozenset({LoopState.REQUESTING_MODEL}),
    LoopState.REQUESTING_MODEL: frozenset({LoopState.STREAMING_MODEL}),
    LoopState.STREAMING_MODEL: frozenset(
        {
            LoopState.REQUESTING_MODEL,
            LoopState.COLLECTING_TOOL_CALLS,
            LoopState.VALIDATING_COMPLETION,
        }
    ),
    LoopState.COLLECTING_TOOL_CALLS: frozenset(
        {LoopState.REQUESTING_MODEL, LoopState.AWAITING_APPROVAL, LoopState.EXECUTING_TOOLS}
    ),
    LoopState.AWAITING_APPROVAL: frozenset({LoopState.EXECUTING_TOOLS, LoopState.WAITING_FOR_USER}),
    LoopState.EXECUTING_TOOLS: frozenset(
        {LoopState.APPENDING_TOOL_RESULTS, LoopState.RECONCILIATION_REQUIRED}
    ),
    LoopState.APPENDING_TOOL_RESULTS: frozenset({LoopState.REQUESTING_MODEL, LoopState.WAITING_FOR_USER}),
    LoopState.VALIDATING_COMPLETION: frozenset({LoopState.REQUESTING_MODEL, LoopState.COMPLETED}),
    LoopState.WAITING_FOR_USER: frozenset(),
    LoopState.RECONCILIATION_REQUIRED: frozenset(),
    LoopState.COMPLETED: frozenset(),
    LoopState.CANCELLED: frozenset(),
    LoopState.FAILED: frozenset(),
}


class LoopLimits(_LoopContract):
    request_count: int | None = Field(default=None, ge=1)
    tool_calls: int | None = Field(default=None, ge=0)
    completion_retries: int = Field(default=2, ge=0)
    model_retries: int = Field(default=5, ge=0)
    model_retry_delay_seconds: float = Field(default=2.0, ge=0)
    model_retry_max_delay_seconds: float = Field(default=60.0, gt=0)
    output_limit_retries: int = Field(default=3, ge=0)
    model_request_timeout_seconds: float | None = Field(default=None, gt=0)
    model_stream_idle_timeout_seconds: float = Field(default=300.0, gt=0)
    parallel_tool_calls: bool = False


class LoopTransition(_LoopContract):
    kind: Literal["state_changed"] = "state_changed"
    sequence: int = Field(ge=0)
    previous: LoopState
    current: LoopState
    reason: str = Field(max_length=512)
    request_index: int = Field(ge=0)


class LoopTextEmitted(_LoopContract):
    kind: Literal["text_emitted"] = "text_emitted"
    sequence: int = Field(ge=0)
    text: str
    request_index: int = Field(ge=1)


class LoopTextRetracted(_LoopContract):
    kind: Literal["text_retracted"] = "text_retracted"
    sequence: int = Field(ge=0)
    characters: int = Field(ge=1)
    request_index: int = Field(ge=1)


class LoopThinkingEmitted(_LoopContract):
    kind: Literal["thinking_emitted"] = "thinking_emitted"
    sequence: int = Field(ge=0)
    text: str
    request_index: int = Field(ge=1)


class LoopCommentaryEmitted(_LoopContract):
    kind: Literal["commentary_emitted"] = "commentary_emitted"
    sequence: int = Field(ge=0)
    text: str
    request_index: int = Field(ge=1)


class LoopToolCallPrepared(_LoopContract):
    kind: Literal["tool_call_prepared"] = "tool_call_prepared"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    order: int = Field(ge=0)
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict[str, Any])
    origin: str = "unregistered capability"
    risk: str = "external_unknown"


class LoopToolResultRecorded(_LoopContract):
    kind: Literal["tool_result_recorded"] = "tool_result_recorded"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    order: int = Field(ge=0)
    call_id: str
    name: str
    status: str
    canonical_output: Any = None
    model_output: str
    error: str | None = None
    effect_receipt_id: str | None = None
    idempotency_key: str
    call_view: dict[str, Any] | None = None
    result_view: dict[str, Any] | None = None


class LoopStallObserved(_LoopContract):
    kind: Literal["stall_observed"] = "stall_observed"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    call_id: str
    signature: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    window_count: int = Field(ge=2)


class LoopUsageObserved(_LoopContract):
    kind: Literal["usage_observed"] = "usage_observed"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)


class LoopRetryScheduled(_LoopContract):
    kind: Literal["retry_scheduled"] = "retry_scheduled"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    attempt: int = Field(ge=1)
    category: str = Field(max_length=128)
    delay_seconds: float = Field(default=0, ge=0)
    discarded_text_characters: int = Field(default=0, ge=0)
    discarded_thinking_characters: int = Field(default=0, ge=0)


class LoopRequestAttempted(_LoopContract):
    kind: Literal["request_attempted"] = "request_attempted"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    model_attempts: int = Field(ge=1)


class LoopCompletionDecided(_LoopContract):
    kind: Literal["completion_decided"] = "completion_decided"
    sequence: int = Field(ge=0)
    request_index: int = Field(ge=1)
    accepted: bool
    issues: tuple[str, ...] = Field(default_factory=tuple, max_length=128)


LoopEvent = Annotated[
    LoopTransition
    | LoopTextEmitted
    | LoopTextRetracted
    | LoopThinkingEmitted
    | LoopCommentaryEmitted
    | LoopToolCallPrepared
    | LoopToolResultRecorded
    | LoopStallObserved
    | LoopUsageObserved
    | LoopRetryScheduled
    | LoopRequestAttempted
    | LoopCompletionDecided,
    Field(discriminator="kind"),
]
LoopEventSink = Callable[[LoopEvent], Awaitable[None]]
CompletionEvaluator = Callable[
    [str], Sequence[str | CompletionBlocker] | Awaitable[Sequence[str | CompletionBlocker]]
]
ApprovalBatchHandler = Callable[[tuple[ApprovalRequest, ...]], Awaitable[Mapping[str, CapabilityApproval]]]
CapabilityVisibility = Callable[[str], bool]


class LoopUsageTotals(_LoopContract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class LoopOutcome(_LoopContract):
    state: Literal[LoopState.COMPLETED] = LoopState.COMPLETED
    output: str
    thinking: str = ""
    stop_reason: ModelStopReason
    request_count: int = Field(ge=1)
    model_attempts: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    usage: LoopUsageTotals = Field(default_factory=LoopUsageTotals)
    transitions: tuple[LoopTransition, ...]
    response: Any | None = None


class LoopWaitingOutcome(_LoopContract):
    state: Literal[LoopState.WAITING_FOR_USER] = LoopState.WAITING_FOR_USER
    output: Literal[""] = ""
    thinking: Literal[""] = ""
    request_count: int = Field(ge=1)
    model_attempts: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    usage: LoopUsageTotals = Field(default_factory=LoopUsageTotals)
    transitions: tuple[LoopTransition, ...]


@dataclass(frozen=True, slots=True)
class _StreamedResponse(Generic[MessageT]):
    text: str
    thinking: str
    tool_calls: tuple[str, ...]
    completed_tool_calls: Mapping[str, ModelToolCallCompleted]
    stop_reason: ModelStopReason | None
    response: MessageT | None


@dataclass(frozen=True, slots=True)
class LoopContinuation(Generic[MessageT]):
    request_index: int
    prior_request: ModelDriverRequest[MessageT]
    candidate_output: str
    thinking: str
    completion_issues: tuple[str, ...]
    response: MessageT | None = None


@dataclass(frozen=True, slots=True)
class LoopTruncationContinuation(Generic[MessageT]):
    """A safe request-boundary retry after the provider exhausts output.

    No tool in the truncated response has executed. ``incomplete_tool_calls``
    distinguishes an invalid partial call, which must be regenerated, from a
    text-only response that a Runtime may continue from its exact response.
    """

    request_index: int
    prior_request: ModelDriverRequest[MessageT]
    partial_output: str
    thinking: str
    incomplete_tool_calls: tuple[str, ...]
    output_tokens: int
    response: MessageT | None = None


RequestContinuation = Callable[
    [LoopContinuation[MessageT]],
    ModelDriverRequest[MessageT] | Awaitable[ModelDriverRequest[MessageT]],
]

TruncationRequestContinuation = Callable[
    [LoopTruncationContinuation[MessageT]],
    ModelDriverRequest[MessageT]
    | None
    | Awaitable[ModelDriverRequest[MessageT] | None],
]

InteractiveContinuation = Callable[
    [LoopContinuation[MessageT]],
    ModelDriverRequest[MessageT]
    | None
    | Awaitable[ModelDriverRequest[MessageT] | None],
]


@dataclass(frozen=True, slots=True)
class LoopToolContinuation(Generic[MessageT]):
    request_index: int
    prior_request: ModelDriverRequest[MessageT]
    assistant_text: str
    thinking: str
    results: tuple[CapabilityResult, ...]
    response: MessageT | None = None


ToolRequestContinuation = Callable[
    [LoopToolContinuation[MessageT]],
    ModelDriverRequest[MessageT] | None | Awaitable[ModelDriverRequest[MessageT] | None],
]


@dataclass(frozen=True, slots=True)
class LoopSuspendedContinuation(Generic[MessageT]):
    request_index: int
    prior_request: ModelDriverRequest[MessageT]
    response: MessageT


SuspendedRequestContinuation = Callable[
    [LoopSuspendedContinuation[MessageT]],
    ModelDriverRequest[MessageT] | Awaitable[ModelDriverRequest[MessageT]],
]


class LumenAgentLoopError(RuntimeError):
    """Base failure carrying the already streamed, non-durable partial text."""

    def __init__(self, message: str, *, partial_output: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.partial_output = partial_output
        self.retryable = retryable
        self.usage = LoopUsageTotals()
        self.request_count = 0
        self.model_attempts = 0


class LoopProtocolError(LumenAgentLoopError):
    pass


class LoopBudgetExceeded(LumenAgentLoopError):
    pass


class LoopProviderFailure(LumenAgentLoopError):
    pass


class LoopRequestTimeout(LumenAgentLoopError):
    """Explicit total deadline, independent of usage and idle timeouts."""


class LoopContextOverflow(LumenAgentLoopError):
    """Provider rejected the context; only a changed request may recover."""


class LoopCompletionRejected(LumenAgentLoopError):
    pass


class LoopToolCallsUnsupported(LumenAgentLoopError):
    pass


class LoopTruncated(LumenAgentLoopError):
    pass


async def _no_emit(_event: LoopEvent) -> None:
    return None


def _accept_completion(_output: str) -> tuple[str, ...]:
    return ()


def _always_visible(_name: str) -> bool:
    return True


ResolvedT = TypeVar("ResolvedT")


async def _resolve(value: ResolvedT | Awaitable[ResolvedT]) -> ResolvedT:
    if inspect.isawaitable(value):
        return await cast(Awaitable[ResolvedT], value)
    return value


class LumenAgentLoop(Generic[MessageT]):
    """Lumen-owned explicit loop state machine.

    The class is single-run-at-a-time. It owns provider stream ordering,
    bounded retry, cancellation, and terminal-candidate validation. It does
    not own Session persistence, Context assembly, or capability policy.
    """

    def __init__(
        self,
        driver: ModelDriver[MessageT],
        *,
        completion_evaluator: CompletionEvaluator | None = None,
        limits: LoopLimits | None = None,
        capability_gateway: CapabilityGateway | None = None,
        capability_visible: CapabilityVisibility | None = None,
    ) -> None:
        self._driver = driver
        self._completion_evaluator: CompletionEvaluator = completion_evaluator or _accept_completion
        self._limits = limits or LoopLimits()
        self._capability_gateway = capability_gateway
        self._capability_visible: CapabilityVisibility = capability_visible or _always_visible
        self._state = LoopState.PREPARING
        self._running = False
        self._event_sequence: int = 0
        self._request_index: int = 0
        self._model_attempts = 0
        self._transitions: list[LoopTransition] = []
        self._completed_usage = LoopUsageTotals()
        self._active_request_usage = LoopUsageTotals()
        self._stall_window: list[str] = []
        self._suspended_response: MessageT | None = None

    @property
    def state(self) -> LoopState:
        return self._state

    @staticmethod
    def validate_transition(previous: LoopState, current: LoopState) -> None:
        if current in {LoopState.CANCELLED, LoopState.FAILED} and previous not in _TERMINAL_STATES:
            return
        if current not in _ALLOWED_TRANSITIONS[previous]:
            raise LoopProtocolError(f"illegal loop transition: {previous.value} -> {current.value}")

    async def _emit(self, sink: LoopEventSink, event: LoopEvent) -> None:
        await sink(event)
        self._event_sequence += 1

    async def _transition(self, current: LoopState, reason: str, sink: LoopEventSink) -> None:
        previous = self._state
        self.validate_transition(previous, current)
        transition = LoopTransition(
            sequence=self._event_sequence,
            previous=previous,
            current=current,
            reason=reason,
            request_index=self._request_index,
        )
        self._state = current
        self._transitions.append(transition)
        await self._emit(sink, transition)

    def _commit_request_usage(self) -> None:
        self._completed_usage = LoopUsageTotals(
            input_tokens=(
                self._completed_usage.input_tokens + self._active_request_usage.input_tokens
            ),
            output_tokens=(
                self._completed_usage.output_tokens + self._active_request_usage.output_tokens
            ),
            cache_read_tokens=(
                self._completed_usage.cache_read_tokens
                + self._active_request_usage.cache_read_tokens
            ),
            cache_write_tokens=(
                self._completed_usage.cache_write_tokens
                + self._active_request_usage.cache_write_tokens
            ),
        )
        self._active_request_usage = LoopUsageTotals()

    async def _fail(self, error: LumenAgentLoopError, sink: LoopEventSink) -> NoReturn:
        error.usage = LoopUsageTotals(
            input_tokens=(self._completed_usage.input_tokens + self._active_request_usage.input_tokens),
            output_tokens=(self._completed_usage.output_tokens + self._active_request_usage.output_tokens),
            cache_read_tokens=(
                self._completed_usage.cache_read_tokens + self._active_request_usage.cache_read_tokens
            ),
            cache_write_tokens=(
                self._completed_usage.cache_write_tokens + self._active_request_usage.cache_write_tokens
            ),
        )
        error.request_count = self._request_index
        error.model_attempts = self._model_attempts
        if self._state not in _TERMINAL_STATES:
            await self._transition(LoopState.FAILED, type(error).__name__, sink)
        raise error

    async def _emit_prepared_calls(
        self,
        gateway: CapabilityGateway,
        capabilities: Sequence[PreparedCapability],
        sink: LoopEventSink,
    ) -> None:
        for order, prepared in enumerate(capabilities):
            invocation = prepared.invocation
            descriptor = gateway.descriptor(invocation.name)
            await self._emit(
                sink,
                LoopToolCallPrepared(
                    sequence=self._event_sequence,
                    request_index=self._request_index,
                    order=order,
                    call_id=invocation.provider_call_id,
                    name=invocation.name,
                    arguments=invocation.arguments,
                    origin=(descriptor.origin if descriptor is not None else "unregistered capability"),
                    risk=(descriptor.risk if descriptor is not None else "external_unknown"),
                ),
            )

    async def _approval_decisions(
        self,
        gateway: CapabilityGateway,
        capabilities: Sequence[PreparedCapability],
        *,
        sink: LoopEventSink,
        approve: GatewayApprovalHandler | None,
        approve_batch: ApprovalBatchHandler | None,
    ) -> Mapping[str, CapabilityApproval]:
        requests = tuple(
            request
            for prepared in capabilities
            if prepared.denial_message is None
            if (request := gateway.approval_request(prepared.invocation)) is not None
        )
        if not requests:
            return {}
        await self._transition(LoopState.AWAITING_APPROVAL, "approval_required", sink)
        if approve_batch is not None and len(requests) > 1:
            return await approve_batch(requests)
        if approve is None:
            return {}
        return {request.call_id: await approve(request) for request in requests}

    @staticmethod
    async def _invoke_capability(
        gateway: CapabilityGateway,
        prepared: PreparedCapability,
        decision: CapabilityApproval | None,
    ) -> CapabilityResult:
        if decision is None:
            return await gateway.invoke_prepared(prepared)

        async def resolved(_request: ApprovalRequest) -> CapabilityApproval:
            return decision

        return await gateway.invoke_prepared(prepared, approve=resolved)

    def _tool_batches(
        self,
        gateway: CapabilityGateway,
        capabilities: Sequence[PreparedCapability],
    ) -> tuple[tuple[PreparedCapability, ...], ...]:
        if not self._limits.parallel_tool_calls:
            return tuple((prepared,) for prepared in capabilities)
        batches: list[tuple[PreparedCapability, ...]] = []
        parallel_batch: list[PreparedCapability] = []
        for prepared in capabilities:
            if (
                prepared.denial_message is None
                and gateway.concurrency_for(prepared.invocation) is ToolConcurrency.PARALLEL_SAFE
            ):
                parallel_batch.append(prepared)
                continue
            if parallel_batch:
                batches.append(tuple(parallel_batch))
                parallel_batch = []
            batches.append((prepared,))
        if parallel_batch:
            batches.append(tuple(parallel_batch))
        return tuple(batches)

    async def _execute_tools(
        self,
        gateway: CapabilityGateway,
        invocations: Sequence[CapabilityInvocation],
        *,
        sink: LoopEventSink,
        approve: GatewayApprovalHandler | None,
        approve_batch: ApprovalBatchHandler | None,
    ) -> tuple[CapabilityResult, ...]:
        capabilities = tuple(
            [
                PreparedCapability(invocation, "tool_not_loaded: discover this deferred tool first")
                if not self._capability_visible(invocation.name)
                else await gateway.prepare(invocation)
                for invocation in invocations
            ]
        )
        await self._emit_prepared_calls(gateway, capabilities, sink)
        decisions = await self._approval_decisions(
            gateway,
            capabilities,
            sink=sink,
            approve=approve,
            approve_batch=approve_batch,
        )
        await self._transition(LoopState.EXECUTING_TOOLS, "tool_batch_ready", sink)
        results: list[CapabilityResult] = []
        try:
            for batch in self._tool_batches(gateway, capabilities):
                if len(batch) == 1:
                    prepared = batch[0]
                    results.append(
                        await self._invoke_capability(
                            gateway,
                            prepared,
                            decisions.get(prepared.invocation.provider_call_id),
                        )
                    )
                    continue
                tasks = [
                    asyncio.create_task(
                        self._invoke_capability(
                            gateway,
                            prepared,
                            decisions.get(prepared.invocation.provider_call_id),
                        )
                    )
                    for prepared in batch
                ]
                try:
                    results.extend(await asyncio.gather(*tasks))
                except asyncio.CancelledError:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    settled = await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
                    results.extend(item for item in settled if isinstance(item, CapabilityResult))
                    raise
        except asyncio.CancelledError:
            if results:
                await asyncio.shield(self._emit_tool_results(results, sink=sink))
            raise
        return tuple(results)

    @staticmethod
    def _tool_signature(result: CapabilityResult, model_output: str) -> str:
        payload = json.dumps(
            {
                "name": result.invocation.name,
                "arguments": result.invocation.arguments,
                "status": result.status.value,
                "result": model_output,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    async def _record_tool_results(
        self,
        results: Sequence[CapabilityResult],
        *,
        candidate: str,
        sink: LoopEventSink,
    ) -> None:
        await self._transition(LoopState.APPENDING_TOOL_RESULTS, "tool_results_ready", sink)
        if candidate:
            await self._emit(
                sink,
                LoopTextRetracted(
                    sequence=self._event_sequence,
                    characters=len(candidate),
                    request_index=self._request_index,
                ),
            )
            await self._emit(
                sink,
                LoopCommentaryEmitted(
                    sequence=self._event_sequence,
                    text=candidate,
                    request_index=self._request_index,
                ),
            )
        await self._emit_tool_results(results, sink=sink)

    async def _emit_tool_results(
        self,
        results: Sequence[CapabilityResult],
        *,
        sink: LoopEventSink,
    ) -> None:
        """Publish completed receipts, including calls that finished before cancellation."""

        for order, result in enumerate(results):
            model_output = result.model_output or result.error or "capability failed"
            await self._emit(
                sink,
                LoopToolResultRecorded(
                    sequence=self._event_sequence,
                    request_index=self._request_index,
                    order=order,
                    call_id=result.invocation.provider_call_id,
                    name=result.invocation.name,
                    status=result.status.value,
                    canonical_output=result.output,
                    model_output=model_output,
                    error=result.error,
                    effect_receipt_id=result.effect_receipt_id,
                    idempotency_key=result.idempotency_key,
                    call_view=result.call_view,
                    result_view=result.result_view,
                ),
            )
            signature = self._tool_signature(result, model_output)
            window_count = self._stall_window.count(signature) + 1
            if window_count >= 2:
                await self._emit(
                    sink,
                    LoopStallObserved(
                        sequence=self._event_sequence,
                        request_index=self._request_index,
                        call_id=result.invocation.provider_call_id,
                        signature=signature,
                        window_count=window_count,
                    ),
                )
            self._stall_window.append(signature)
            self._stall_window = self._stall_window[-8:]

    async def _continue_after_tool_calls(
        self,
        *,
        gateway: CapabilityGateway,
        continuation: ToolRequestContinuation[MessageT],
        execution_id: str,
        calls: Sequence[ModelToolCallCompleted],
        current_request: ModelDriverRequest[MessageT],
        candidate: str,
        thinking: str,
        sink: LoopEventSink,
        approve: GatewayApprovalHandler | None,
        approve_batch: ApprovalBatchHandler | None,
        response: MessageT | None,
    ) -> ModelDriverRequest[MessageT] | None:
        invocations = tuple(
            CapabilityInvocation(
                execution_id=f"{execution_id}:step-{self._request_index}",
                provider_call_id=call.call_id,
                name=call.name,
                arguments=call.arguments,
            )
            for call in calls
        )
        results = await self._execute_tools(
            gateway,
            invocations,
            sink=sink,
            approve=approve,
            approve_batch=approve_batch,
        )
        await self._record_tool_results(results, candidate=candidate, sink=sink)
        return await _resolve(
            continuation(
                LoopToolContinuation[MessageT](
                    request_index=self._request_index,
                    prior_request=current_request,
                    assistant_text=candidate,
                    thinking=thinking,
                    results=results,
                    response=response,
                )
            )
        )

    async def _handle_cancellation(self, sink: LoopEventSink) -> None:
        if self._suspended_response is not None:
            try:
                await self._driver.cancel_suspended_response(self._suspended_response)
            except Exception:
                pass
        state = self._state
        if state is LoopState.EXECUTING_TOOLS:
            await self._transition(
                LoopState.RECONCILIATION_REQUIRED,
                "cancelled_during_tool_execution",
                sink,
            )
        elif state not in _TERMINAL_STATES:
            await self._transition(LoopState.CANCELLED, "cancelled", sink)

    async def _raw_driver_events(
        self,
        request: ModelDriverRequest[MessageT],
        response_holder: list[MessageT | None],
    ) -> AsyncGenerator[ModelStreamEvent, None]:
        async with self._driver.open_stream(request) as stream:
            try:
                async for event in stream.events:
                    yield event
            finally:
                response_holder.append(stream.response)

    async def _driver_events(
        self,
        request: ModelDriverRequest[MessageT],
        response_holder: list[MessageT | None],
    ) -> AsyncIterator[ModelStreamEvent]:
        # HTTP-backed drivers enforce idle on raw bytes (including SSE pings).
        # Other drivers use event-idle. Never time the consumer/UI's processing.
        idle = self._limits.model_stream_idle_timeout_seconds
        timeout = None if getattr(self._driver, "manages_stream_idle_timeout", False) else idle
        events = self._raw_driver_events(
            replace(request, stream_idle_timeout_seconds=idle), response_holder,
        )
        sequence = 0
        try:
            while True:
                deadline = asyncio.timeout(timeout)
                try:
                    async with deadline:
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if not deadline.expired():
                        raise
                    if sequence == 0:
                        yield ModelResponseStarted(sequence=0)
                        sequence = 1
                    yield ModelProviderError(
                        sequence=sequence,
                        category="stream_idle_timeout",
                        message=f"Model stream received no activity for {idle:g}s.",
                        retryable=True,
                    )
                    return
                sequence = event.sequence + 1
                yield event
        finally:
            await events.aclose()

    async def _stream_request(
        self,
        request: ModelDriverRequest[MessageT],
        *,
        partial_output: str,
        sink: LoopEventSink,
    ) -> _StreamedResponse[MessageT]:
        provider_attempt = 0
        while True:
            self._model_attempts += 1
            await self._emit(
                sink,
                LoopRequestAttempted(
                    sequence=self._event_sequence,
                    request_index=self._request_index,
                    model_attempts=self._model_attempts,
                ),
            )
            expected_sequence = 0
            response_started = False
            response_completed = False
            candidate_text: list[str] = []
            candidate_thinking: list[str] = []
            tool_calls: dict[str, str] = {}
            completed_tool_calls: dict[str, ModelToolCallCompleted] = {}
            self._active_request_usage = LoopUsageTotals()
            stop_reason: ModelStopReason | None = None
            provider_error: ModelProviderError | None = None
            response_holder: list[MessageT | None] = []

            async for event in self._driver_events(request, response_holder):
                if event.sequence != expected_sequence:
                    await self._fail(
                        LoopProtocolError(
                            "provider event sequence mismatch: "
                            f"expected {expected_sequence}, received {event.sequence}",
                            partial_output=partial_output + "".join(candidate_text),
                        ),
                        sink,
                    )
                expected_sequence += 1
                if response_completed:
                    await self._fail(
                        LoopProtocolError(
                            "provider emitted an event after its terminal event",
                            partial_output=partial_output + "".join(candidate_text),
                        ),
                        sink,
                    )

                if isinstance(event, ModelResponseStarted):
                    if response_started or event.sequence != 0:
                        await self._fail(
                            LoopProtocolError("response_started must be the first event"), sink
                        )
                    response_started = True
                    await self._transition(
                        LoopState.STREAMING_MODEL,
                        "provider_stream_started",
                        sink,
                    )
                elif not response_started:
                    await self._fail(
                        LoopProtocolError("provider stream did not start with response_started"),
                        sink,
                    )
                elif isinstance(event, ModelTextDelta):
                    candidate_text.append(event.content)
                    await self._emit(
                        sink,
                        LoopTextEmitted(
                            sequence=self._event_sequence,
                            text=event.content,
                            request_index=self._request_index,
                        ),
                    )
                elif isinstance(event, ModelThinkingDelta):
                    candidate_thinking.append(event.content)
                    await self._emit(
                        sink,
                        LoopThinkingEmitted(
                            sequence=self._event_sequence,
                            text=event.content,
                            request_index=self._request_index,
                        ),
                    )
                elif isinstance(event, ModelUsage):
                    self._active_request_usage = LoopUsageTotals(
                        input_tokens=max(
                            self._active_request_usage.input_tokens,
                            event.input_tokens,
                        ),
                        output_tokens=max(
                            self._active_request_usage.output_tokens,
                            event.output_tokens,
                        ),
                        cache_read_tokens=max(
                            self._active_request_usage.cache_read_tokens,
                            event.cache_read_tokens or 0,
                        ),
                        cache_write_tokens=max(
                            self._active_request_usage.cache_write_tokens,
                            event.cache_write_tokens or 0,
                        ),
                    )
                    await self._emit(
                        sink,
                        LoopUsageObserved(
                            sequence=self._event_sequence,
                            request_index=self._request_index,
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                            cache_read_tokens=event.cache_read_tokens,
                            cache_write_tokens=event.cache_write_tokens,
                        ),
                    )
                elif isinstance(event, ModelToolCallStarted):
                    if event.call_id in tool_calls:
                        await self._fail(
                            LoopProtocolError(f"duplicate tool call id: {event.call_id}"), sink
                        )
                    tool_calls[event.call_id] = event.name
                    if self._state is LoopState.STREAMING_MODEL:
                        await self._transition(
                            LoopState.COLLECTING_TOOL_CALLS,
                            "tool_call_started",
                            sink,
                        )
                elif isinstance(event, ModelToolArgumentsDelta):
                    if event.call_id not in tool_calls:
                        await self._fail(
                            LoopProtocolError(
                                f"tool arguments precede tool call start: {event.call_id}"
                            ),
                            sink,
                        )
                elif isinstance(event, ModelToolCallCompleted):
                    if event.call_id not in tool_calls:
                        await self._fail(
                            LoopProtocolError(
                                f"completed tool call has no start event: {event.call_id}"
                            ),
                            sink,
                        )
                    if tool_calls[event.call_id] != event.name:
                        await self._fail(
                            LoopProtocolError(
                                f"tool call name changed for {event.call_id}: "
                                f"{tool_calls[event.call_id]} -> {event.name}"
                            ),
                            sink,
                        )
                    completed_tool_calls[event.call_id] = event
                elif isinstance(event, ModelProviderError):
                    response_completed = True
                    provider_error = event
                else:
                    response_completed = True
                    stop_reason = event.stop_reason

            response_message = response_holder[-1] if response_holder else None
            if not response_completed:
                provider_error = ModelProviderError(
                    sequence=expected_sequence,
                    category="stream_disconnected",
                    message="Provider stream ended before its terminal event.",
                    retryable=True,
                )
            if provider_error is None:
                return _StreamedResponse(
                    text="".join(candidate_text),
                    thinking="".join(candidate_thinking),
                    tool_calls=tuple(tool_calls),
                    completed_tool_calls=completed_tool_calls,
                    stop_reason=stop_reason,
                    response=response_message,
                )
            if provider_error.category == "context_overflow" and provider_error.replay_safe:
                discarded = len("".join(candidate_text))
                if discarded:
                    await self._emit(sink, LoopTextRetracted(
                        sequence=self._event_sequence, characters=discarded,
                        request_index=self._request_index,
                    ))
                self._commit_request_usage()
                raise LoopContextOverflow(provider_error.message, partial_output=partial_output)
            if (
                provider_error.retryable
                and provider_error.replay_safe
                and provider_attempt < self._limits.model_retries
                and (
                    provider_error.retry_after_seconds is None
                    or provider_error.retry_after_seconds <= self._limits.model_retry_max_delay_seconds
                )
            ):
                self._commit_request_usage()
                provider_attempt += 1
                delay = min(
                    self._limits.model_retry_max_delay_seconds,
                    self._limits.model_retry_delay_seconds * 2 ** (provider_attempt - 1)
                    * random.uniform(0.8, 1.2),
                )
                if provider_error.retry_after_seconds is not None:
                    delay = max(delay, provider_error.retry_after_seconds)
                discarded_text = len("".join(candidate_text))
                if discarded_text:
                    await self._emit(
                        sink,
                        LoopTextRetracted(
                            sequence=self._event_sequence,
                            characters=discarded_text,
                            request_index=self._request_index,
                        ),
                    )
                await self._emit(
                    sink,
                    LoopRetryScheduled(
                        sequence=self._event_sequence,
                        request_index=self._request_index,
                        attempt=provider_attempt,
                        category=provider_error.category,
                        delay_seconds=delay,
                        discarded_text_characters=discarded_text,
                        discarded_thinking_characters=len("".join(candidate_thinking)),
                    ),
                )
                if self._state is not LoopState.REQUESTING_MODEL:
                    await self._transition(LoopState.REQUESTING_MODEL, "provider_retry", sink)
                await asyncio.sleep(delay)
                continue
            await self._fail(
                LoopProviderFailure(
                    provider_error.message,
                    partial_output=partial_output + "".join(candidate_text),
                    retryable=provider_error.retryable and provider_error.replay_safe,
                ),
                sink,
            )

    async def _request_model(
        self,
        request: ModelDriverRequest[MessageT],
        partial_output: str,
        sink: LoopEventSink,
        compact_request: Callable[
            [ModelDriverRequest[MessageT], int], Awaitable[ModelDriverRequest[MessageT] | None]
        ] | None,
    ) -> tuple[ModelDriverRequest[MessageT], _StreamedResponse[MessageT]]:
        """Own logical request limits, deadlines and one overflow recovery."""
        overflow_recovery_attempted = False
        while True:
            if (
                self._limits.request_count is not None
                and self._request_index >= self._limits.request_count
            ):
                await self._fail(
                    LoopBudgetExceeded(
                        f"model request limit reached: {self._limits.request_count}",
                        partial_output=partial_output,
                    ),
                    sink,
                )
            self._request_index += 1
            await self._transition(LoopState.REQUESTING_MODEL, "model_request", sink)
            if request.input_manifest.step != self._request_index:
                await self._fail(
                    LoopProtocolError(
                        "model input manifest step does not match loop request index: "
                        f"{request.input_manifest.step} != {self._request_index}"
                    ),
                    sink,
                )

            deadline = asyncio.timeout(self._limits.model_request_timeout_seconds)
            try:
                async with deadline:
                    streamed = await self._stream_request(
                        request,
                        partial_output=partial_output,
                        sink=sink,
                    )
            except TimeoutError:
                if not deadline.expired():
                    raise
                await self._fail(
                    LoopRequestTimeout(
                        "model request deadline reached: "
                        f"{self._limits.model_request_timeout_seconds:g}s; "
                        "stream cancelled; no collected tool calls were executed. "
                        "The explicit agent.limits.model_request_timeout_seconds "
                        "deadline was reached; request/tool budgets were not exhausted.",
                        partial_output=partial_output,
                    ),
                    sink,
                )
            except LoopContextOverflow as error:
                if not overflow_recovery_attempted and compact_request is not None:
                    recovered_request = await compact_request(request, self._request_index + 1)
                    if recovered_request is not None:
                        overflow_recovery_attempted = True
                        request = recovered_request
                        continue
                await self._fail(error, sink)
            return request, streamed

    async def run(
        self,
        request: ModelDriverRequest[MessageT],
        *,
        emit: LoopEventSink | None = None,
        continue_request: RequestContinuation[MessageT] | None = None,
        continue_truncated: TruncationRequestContinuation[MessageT] | None = None,
        continue_for_input: InteractiveContinuation[MessageT] | None = None,
        continue_after_tools: ToolRequestContinuation[MessageT] | None = None,
        continue_suspended: SuspendedRequestContinuation[MessageT] | None = None,
        compact_request: Callable[
            [ModelDriverRequest[MessageT], int], Awaitable[ModelDriverRequest[MessageT] | None]
        ] | None = None,
        execution_id: str | None = None,
        approve: GatewayApprovalHandler | None = None,
        approve_batch: ApprovalBatchHandler | None = None,
    ) -> LoopOutcome | LoopWaitingOutcome:
        if self._running:
            raise RuntimeError("LumenAgentLoop does not allow concurrent runs")
        self._running = True
        self._state = LoopState.PREPARING
        self._event_sequence = 0
        self._request_index = 0
        self._model_attempts = 0
        self._transitions = []
        self._completed_usage = LoopUsageTotals()
        self._active_request_usage = LoopUsageTotals()
        self._stall_window = []
        self._suspended_response = None
        sink = emit or _no_emit
        current_request = request
        completion_retries = 0
        partial_output = ""
        tool_call_count = 0
        generation_continuations = 0
        background_polls = 0
        output_limit_retries = 0
        partial_thinking = ""
        try:
            while True:
                current_request, streamed = await self._request_model(
                    current_request, partial_output, sink, compact_request,
                )
                candidate_text = [streamed.text]
                candidate_thinking = [streamed.thinking]
                tool_calls = streamed.tool_calls
                completed_tool_calls = streamed.completed_tool_calls
                stop_reason = streamed.stop_reason
                response_message = streamed.response
                request_output_tokens = self._active_request_usage.output_tokens
                self._commit_request_usage()
                candidate = "".join(candidate_text)
                candidate_reasoning = "".join(candidate_thinking)
                if response_message is not None:
                    if self._suspended_response is not None:
                        response_message = self._driver.merge_responses(
                            self._suspended_response,
                            response_message,
                        )
                    projected_candidate = self._driver.response_text(response_message)
                    projected_reasoning = self._driver.response_thinking(response_message)
                    if projected_candidate != candidate:
                        if candidate:
                            await self._emit(
                                sink,
                                LoopTextRetracted(
                                    sequence=self._event_sequence,
                                    characters=len(candidate),
                                    request_index=self._request_index,
                                ),
                            )
                        if projected_candidate:
                            await self._emit(
                                sink,
                                LoopTextEmitted(
                                    sequence=self._event_sequence,
                                    text=projected_candidate,
                                    request_index=self._request_index,
                                ),
                            )
                        candidate = projected_candidate
                    candidate_reasoning = projected_reasoning or candidate_reasoning
                if stop_reason is ModelStopReason.SUSPENDED:
                    if response_message is None or continue_suspended is None:
                        await self._fail(
                            LoopProviderFailure(
                                "suspended response requires an exact response and continuation",
                                partial_output=partial_output + candidate,
                            ),
                            sink,
                        )
                    previous_id = getattr(self._suspended_response, "provider_response_id", None)
                    current_id = getattr(response_message, "provider_response_id", None)
                    if previous_id and previous_id == current_id:
                        background_polls += 1
                        if background_polls > 1000:
                            await self._driver.cancel_suspended_response(response_message)
                            await self._fail(
                                LoopProviderFailure("background response exceeded 1000 polls"), sink
                            )
                    else:
                        generation_continuations += 1
                        if generation_continuations > 10:
                            await self._driver.cancel_suspended_response(response_message)
                            await self._fail(
                                LoopProviderFailure("response exceeded 10 continuations"), sink
                            )
                    if candidate:
                        await self._emit(
                            sink,
                            LoopTextRetracted(
                                sequence=self._event_sequence,
                                characters=len(candidate),
                                request_index=self._request_index,
                            ),
                        )
                    self._suspended_response = response_message
                    delay = self._driver.continuation_delay(response_message)
                    if delay:
                        await asyncio.sleep(delay)
                    assert continue_suspended is not None
                    current_request = await _resolve(
                        continue_suspended(
                            LoopSuspendedContinuation[MessageT](
                                request_index=self._request_index,
                                prior_request=current_request,
                                response=response_message,
                            )
                        )
                    )
                    continue
                if tool_calls:
                    incomplete = set(tool_calls) - set(completed_tool_calls)
                    if stop_reason is ModelStopReason.LENGTH:
                        if (
                            continue_truncated is not None
                            and output_limit_retries < self._limits.output_limit_retries
                        ):
                            next_request = await _resolve(
                                continue_truncated(
                                    LoopTruncationContinuation(
                                        request_index=self._request_index,
                                        prior_request=current_request,
                                        partial_output=candidate,
                                        thinking=candidate_reasoning,
                                        incomplete_tool_calls=tuple(sorted(incomplete or tool_calls)),
                                        output_tokens=request_output_tokens,
                                        response=response_message,
                                    )
                                )
                            )
                            if next_request is not None:
                                output_limit_retries += 1
                                if candidate:
                                    await self._emit(
                                        sink,
                                        LoopTextRetracted(
                                            sequence=self._event_sequence,
                                            characters=len(candidate),
                                            request_index=self._request_index,
                                        ),
                                    )
                                await self._emit(
                                    sink,
                                    LoopRetryScheduled(
                                        sequence=self._event_sequence,
                                        request_index=self._request_index,
                                        attempt=output_limit_retries,
                                        category="output_limit",
                                    ),
                                )
                                current_request = next_request
                                continue
                        await self._fail(
                            LoopTruncated(
                                "provider ended with incomplete tool calls: "
                                + ", ".join(sorted(incomplete or tool_calls)),
                                partial_output=partial_output + candidate,
                                retryable=True,
                            ),
                            sink,
                        )
                    if stop_reason is not ModelStopReason.TOOL_CALL:
                        await self._fail(
                            LoopProviderFailure(
                                "provider returned tool calls with an incompatible stop reason: "
                                f"{stop_reason.value if stop_reason is not None else 'missing'}",
                                partial_output=partial_output + candidate,
                            ),
                            sink,
                        )
                    if incomplete:
                        await self._fail(
                            LoopTruncated(
                                "provider ended with incomplete tool calls: "
                                + ", ".join(sorted(incomplete or tool_calls)),
                                partial_output=partial_output + candidate,
                                retryable=True,
                            ),
                            sink,
                        )
                    gateway = self._capability_gateway
                    tool_continuation = continue_after_tools
                    if gateway is None or tool_continuation is None or execution_id is None:
                        await self._fail(
                            LoopToolCallsUnsupported(
                                "tool calls require CapabilityGateway, execution identity, "
                                "and a tool-result continuation",
                                partial_output=partial_output + candidate,
                            ),
                            sink,
                        )
                    assert gateway is not None
                    assert tool_continuation is not None
                    assert execution_id is not None
                    ordered_calls = tuple(completed_tool_calls[call_id] for call_id in tool_calls)
                    if (
                        self._limits.tool_calls is not None
                        and tool_call_count + len(ordered_calls) > self._limits.tool_calls
                    ):
                        await self._fail(
                            LoopBudgetExceeded(
                                f"tool call limit reached: {self._limits.tool_calls}",
                                partial_output=partial_output + candidate,
                            ),
                            sink,
                        )
                    current_request = await self._continue_after_tool_calls(
                        gateway=gateway,
                        continuation=tool_continuation,
                        execution_id=execution_id,
                        calls=ordered_calls,
                        current_request=current_request,
                        candidate=partial_output + candidate,
                        thinking=partial_thinking + candidate_reasoning,
                        sink=sink,
                        approve=approve,
                        approve_batch=approve_batch,
                        response=response_message,
                    )
                    self._suspended_response = None
                    tool_call_count += len(ordered_calls)
                    partial_output = ""
                    partial_thinking = ""
                    if current_request is None:
                        await self._transition(
                            LoopState.WAITING_FOR_USER,
                            "tool_requested_user_input",
                            sink,
                        )
                        return LoopWaitingOutcome(
                            request_count=self._request_index,
                            model_attempts=self._model_attempts,
                            tool_call_count=tool_call_count,
                            usage=self._completed_usage,
                            transitions=tuple(self._transitions),
                        )
                    continue

                if stop_reason is None:
                    await self._fail(LoopProtocolError("provider completed without a stop reason"), sink)
                assert stop_reason is not None
                if stop_reason is ModelStopReason.LENGTH:
                    if (
                        continue_truncated is not None
                        and output_limit_retries < self._limits.output_limit_retries
                    ):
                        truncated = LoopTruncationContinuation[MessageT](
                            request_index=self._request_index,
                            prior_request=current_request,
                            partial_output=candidate,
                            thinking=candidate_reasoning,
                            incomplete_tool_calls=(),
                            output_tokens=request_output_tokens,
                            response=response_message,
                        )
                        pending_request = continue_truncated(truncated)
                        next_request = await _resolve(pending_request)
                        if next_request is not None:
                            output_limit_retries += 1
                            partial_output += candidate
                            partial_thinking += candidate_reasoning
                            await self._emit(
                                sink,
                                LoopRetryScheduled(
                                    sequence=self._event_sequence,
                                    request_index=self._request_index,
                                    attempt=output_limit_retries,
                                    category="output_limit",
                                ),
                            )
                            current_request = next_request
                            continue
                    await self._fail(
                        LoopTruncated(
                            "provider response reached its output limit",
                            partial_output=partial_output + candidate,
                            retryable=True,
                        ),
                        sink,
                    )
                if stop_reason is not ModelStopReason.END_TURN:
                    await self._fail(
                        LoopProviderFailure(
                            f"provider response cannot complete the run: {stop_reason.value}",
                            partial_output=partial_output + candidate,
                        ),
                        sink,
                    )

                candidate = partial_output + candidate
                candidate_reasoning = partial_thinking + candidate_reasoning
                await self._transition(LoopState.VALIDATING_COMPLETION, "terminal_candidate", sink)
                if continue_for_input is not None:
                    interactive = LoopContinuation[MessageT](
                        request_index=self._request_index,
                        prior_request=current_request,
                        candidate_output=candidate,
                        thinking=candidate_reasoning,
                        completion_issues=(),
                        response=response_message,
                    )
                    pending_interactive = continue_for_input(interactive)
                    interactive_request = await _resolve(pending_interactive)
                    if interactive_request is not None:
                        if candidate:
                            await self._emit(
                                sink,
                                LoopTextRetracted(
                                    sequence=self._event_sequence,
                                    characters=len(candidate),
                                    request_index=self._request_index,
                                ),
                            )
                            await self._emit(
                                sink,
                                LoopCommentaryEmitted(
                                    sequence=self._event_sequence,
                                    text=candidate,
                                    request_index=self._request_index,
                                ),
                            )
                        partial_output = ""
                        current_request = interactive_request
                        continue
                blockers = await _resolve(self._completion_evaluator(candidate))
                issues = tuple(str(item) for item in blockers)
                await self._emit(
                    sink,
                    LoopCompletionDecided(
                        sequence=self._event_sequence,
                        request_index=self._request_index,
                        accepted=not issues,
                        issues=issues,
                    ),
                )
                if not issues:
                    await self._transition(LoopState.COMPLETED, "completion_gate_passed", sink)
                    return LoopOutcome(
                        output=candidate,
                        thinking=candidate_reasoning,
                        stop_reason=stop_reason,
                        request_count=self._request_index,
                        model_attempts=self._model_attempts,
                        tool_call_count=tool_call_count,
                        usage=self._completed_usage,
                        transitions=tuple(self._transitions),
                        response=response_message,
                    )

                if any(
                    isinstance(item, CompletionBlocker) and not item.model_recoverable
                    for item in blockers
                ):
                    await self._fail(
                        LoopCompletionRejected(
                            "completion_recovery_required: " + "; ".join(issues),
                            partial_output=partial_output + candidate,
                        ),
                        sink,
                    )

                continuation_handler = continue_request
                if continuation_handler is None:
                    await self._fail(
                        LoopCompletionRejected(
                            "completion_gate_failed: " + "; ".join(issues),
                            partial_output=partial_output + candidate,
                        ),
                        sink,
                    )
                assert continuation_handler is not None
                if completion_retries >= self._limits.completion_retries:
                    await self._fail(
                        LoopCompletionRejected(
                            "completion_gate_failed: " + "; ".join(issues),
                            partial_output=partial_output + candidate,
                        ),
                        sink,
                    )
                completion_retries += 1
                if candidate:
                    await self._emit(
                        sink,
                        LoopTextRetracted(
                            sequence=self._event_sequence,
                            characters=len(candidate),
                            request_index=self._request_index,
                        ),
                    )
                partial_output = ""
                partial_thinking = ""
                continuation = LoopContinuation[MessageT](
                    request_index=self._request_index,
                    prior_request=current_request,
                    candidate_output=candidate,
                    thinking=candidate_reasoning,
                    completion_issues=issues,
                    response=response_message,
                )
                current_request = await _resolve(continuation_handler(continuation))
        except asyncio.CancelledError:
            await asyncio.shield(self._handle_cancellation(sink))
            raise
        except LumenAgentLoopError:
            raise
        except Exception:
            if self._state not in _TERMINAL_STATES:
                await self._transition(LoopState.FAILED, "unexpected_loop_error", sink)
            raise
        finally:
            self._running = False


__all__ = [
    "ApprovalBatchHandler",
    "CapabilityVisibility",
    "CompletionEvaluator",
    "InteractiveContinuation",
    "LoopBudgetExceeded",
    "LoopCommentaryEmitted",
    "LoopCompletionDecided",
    "LoopCompletionRejected",
    "LoopContextOverflow",
    "LoopContinuation",
    "LoopEvent",
    "LoopEventSink",
    "LoopLimits",
    "LoopOutcome",
    "LoopProtocolError",
    "LoopProviderFailure",
    "LoopRequestAttempted",
    "LoopRequestTimeout",
    "LoopRetryScheduled",
    "LoopStallObserved",
    "LoopState",
    "LoopSuspendedContinuation",
    "LoopTextEmitted",
    "LoopTextRetracted",
    "LoopThinkingEmitted",
    "LoopToolCallPrepared",
    "LoopToolCallsUnsupported",
    "LoopToolContinuation",
    "LoopToolResultRecorded",
    "LoopTransition",
    "LoopTruncated",
    "LoopTruncationContinuation",
    "LoopUsageObserved",
    "LoopUsageTotals",
    "LoopWaitingOutcome",
    "LumenAgentLoop",
    "LumenAgentLoopError",
    "RequestContinuation",
    "SuspendedRequestContinuation",
    "ToolRequestContinuation",
    "TruncationRequestContinuation",
]
