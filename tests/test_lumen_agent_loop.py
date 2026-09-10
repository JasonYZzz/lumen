from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from pydantic import TypeAdapter

from lumen.agent_loop import (
    LoopCompletionDecided,
    LoopContextOverflow,
    LoopContinuation,
    LoopEvent,
    LoopLimits,
    LoopProtocolError,
    LoopProviderFailure,
    LoopRequestObserved,
    LoopRequestTimeout,
    LoopRetryScheduled,
    LoopStallObserved,
    LoopState,
    LoopTextRetracted,
    LoopToolCallsUnsupported,
    LoopToolContinuation,
    LoopToolResultRecorded,
    LoopTransition,
    LoopTruncated,
    LoopTruncationContinuation,
    LumenAgentLoop,
    ModelDriverRequest,
    ModelProviderError,
    ModelResponseCompleted,
    ModelResponseStarted,
    ModelStopReason,
    ModelStreamEvent,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolCallCompleted,
    ModelToolCallStarted,
    ModelUsage,
    ReplayModelDriver,
    ReplayRecording,
)
from lumen.agent_loop.loop import LoopCompletionRejected
from lumen.completion import CompletionBlocker
from lumen.config import PermissionsConfig
from lumen.context import ModelInputManifest, ReplayEligibility
from lumen.events import ApprovalRequest
from lumen.tools.gateway import CapabilityApproval, CapabilityGateway, CapabilityStatus
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import Risk, ToolConcurrency, ToolSpec


class _TestDriverStream:
    def __init__(self, events: AsyncIterator[ModelStreamEvent]) -> None:
        self.events = events
        self.response: dict[str, Any] | None = None


class _StreamDriverMixin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exc_type, exc_value, traceback
        return None

    @asynccontextmanager
    async def open_stream(
        self, request: ModelDriverRequest[dict[str, Any]]
    ) -> AsyncGenerator[_TestDriverStream, None]:
        yield _TestDriverStream(self.stream(request))  # type: ignore[attr-defined]

    def continuation_delay(self, response: dict[str, Any]) -> float | None:
        del response
        return None

    async def cancel_suspended_response(self, response: dict[str, Any]) -> None:
        del response

    def merge_responses(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        del previous
        return current

    def response_text(self, response: dict[str, Any]) -> str:
        return str(response.get("content", ""))

    def response_thinking(self, response: dict[str, Any]) -> str:
        return str(response.get("thinking", ""))


def _manifest(index: int, *, step: int | None = None) -> ModelInputManifest:
    digest = "sha256:" + "0" * 64
    fingerprint = "sha256:" + f"{index:064x}"
    return ModelInputManifest(
        session_id="loop-session",
        step=step or index,
        route="test:model",
        provider="test",
        model="model",
        context_fingerprint="context-one",
        message_count=1,
        tool_count=0,
        instructions_digest=digest,
        message_history_digest=digest,
        tool_schema_digest=digest,
        context_sources_digest=digest,
        stable_prefix_digest=digest,
        dynamic_tail_digest=digest,
        request_fingerprint=fingerprint,
        replay_eligibility=ReplayEligibility.VERIFY_ONLY,
        non_replayable_reasons=("provider_private_framing_not_captured",),
    )


def _request(index: int, *, step: int | None = None) -> ModelDriverRequest[dict[str, Any]]:
    return ModelDriverRequest(
        request_id=f"request-{index}",
        route="test:model",
        messages=({"role": "user", "content": f"prompt-{index}"},),
        instructions="answer",
        tools=(),
        input_manifest=_manifest(index, step=step),
    )


def _recording(index: int, text: str) -> ReplayRecording:
    return ReplayRecording(
        request_fingerprint=_manifest(index).request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0, provider_response_id=f"response-{index}"),
            ModelTextDelta(sequence=1, content=text),
            ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN),
        ),
    )


def _gateway(tmp_path: Path, *specs: ToolSpec) -> CapabilityGateway:
    registry = ToolRegistry(tmp_path)
    registry.add_many(list(specs), origin="test")
    return CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )


def _tool_recording(
    index: int,
    *calls: tuple[str, str, dict[str, Any]],
    text: str = "",
) -> ReplayRecording:
    events: list[ModelStreamEvent] = [ModelResponseStarted(sequence=0)]
    if text:
        events.append(ModelTextDelta(sequence=len(events), content=text))
    for call_id, name, arguments in calls:
        events.append(ModelToolCallStarted(sequence=len(events), call_id=call_id, name=name))
        events.append(
            ModelToolCallCompleted(
                sequence=len(events),
                call_id=call_id,
                name=name,
                arguments=arguments,
            )
        )
    events.append(ModelResponseCompleted(sequence=len(events), stop_reason=ModelStopReason.TOOL_CALL))
    return ReplayRecording(
        request_fingerprint=_manifest(index).request_fingerprint,
        events=tuple(events),
    )


async def test_lumen_agent_loop_completes_text_thinking_and_usage_in_order() -> None:
    request = _request(1)
    recording = ReplayRecording(
        request_fingerprint=request.input_manifest.request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0, provider_response_id="response-one"),
            ModelThinkingDelta(sequence=1, content="considering"),
            ModelTextDelta(sequence=2, content="hello "),
            ModelTextDelta(sequence=3, content="world"),
            ModelUsage(
                sequence=4,
                input_tokens=12,
                output_tokens=3,
                cache_read_tokens=5,
            ),
            ModelResponseCompleted(sequence=5, stop_reason=ModelStopReason.END_TURN),
        ),
    )
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([recording])
    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(driver)
    outcome = await loop.run(request, emit=emit)

    assert outcome.output == "hello world"
    assert outcome.thinking == "considering"
    assert outcome.usage.input_tokens == 12
    assert outcome.usage.cache_read_tokens == 5
    assert outcome.request_count == 1
    assert loop.state is LoopState.COMPLETED
    observation = next(e for e in events if isinstance(e, LoopRequestObserved))
    assert observation.thinking_characters == len("considering")
    assert observation.text_characters == len("hello world")
    assert observation.first_thinking_seconds is not None
    assert observation.first_text_seconds is not None
    assert observation.first_thinking_seconds <= observation.first_text_seconds <= observation.elapsed_seconds
    assert [event.sequence for event in events] == list(range(len(events)))
    assert [transition.current for transition in outcome.transitions] == [
        LoopState.REQUESTING_MODEL,
        LoopState.STREAMING_MODEL,
        LoopState.VALIDATING_COMPLETION,
        LoopState.COMPLETED,
    ]
    adapter: TypeAdapter[LoopEvent] = TypeAdapter(LoopEvent)
    assert all(adapter.validate_python(event.model_dump(mode="json")) == event for event in events)


async def test_completion_gate_retracts_candidate_and_requests_a_new_step() -> None:
    requests = (_request(1), _request(2))
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver(
        [_recording(1, "draft"), _recording(2, "final")]
    )
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    def evaluate(output: str) -> tuple[str, ...]:
        return ("missing evidence",) if output == "draft" else ()

    def continue_request(
        continuation: LoopContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        assert continuation.candidate_output == "draft"
        assert continuation.completion_issues == ("missing evidence",)
        return requests[1]

    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(driver, completion_evaluator=evaluate)
    outcome = await loop.run(requests[0], emit=emit, continue_request=continue_request)

    assert outcome.output == "final"
    assert outcome.request_count == 2
    assert any(isinstance(event, LoopTextRetracted) and event.characters == len("draft") for event in events)
    decisions = [event for event in events if isinstance(event, LoopCompletionDecided)]
    assert [event.accepted for event in decisions] == [False, True]


async def test_external_reconciliation_does_not_retry_the_model_or_rewrite_artifacts() -> None:
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([_recording(1, "saved report")])
    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(
        driver, completion_evaluator=lambda _: [CompletionBlocker("verify remote result", False)],
    )

    def retry(_continuation: LoopContinuation[dict[str, Any]]) -> ModelDriverRequest[dict[str, Any]]:
        raise AssertionError("an operator recovery task cannot be repaired by model retries")

    with pytest.raises(LoopCompletionRejected, match="completion_recovery_required") as error:
        await loop.run(_request(1), continue_request=retry)
    assert error.value.partial_output == "saved report"
    assert error.value.request_count == 1


class _FlakyDriver(_StreamDriverMixin):
    def __init__(self, *, fail_after_text: bool = False, usage_before_error: bool = False) -> None:
        self.calls = 0
        self.fail_after_text = fail_after_text
        self.usage_before_error = usage_before_error

    async def stream(self, request: ModelDriverRequest[dict[str, Any]]) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.calls += 1
        yield ModelResponseStarted(sequence=0)
        if self.calls == 1:
            if self.fail_after_text:
                yield ModelTextDelta(sequence=1, content="partial")
                yield ModelProviderError(
                    sequence=2,
                    category="connection",
                    message="lost",
                    retryable=True,
                )
            else:
                if self.usage_before_error:
                    yield ModelUsage(sequence=1, input_tokens=7, output_tokens=0)
                yield ModelProviderError(
                    sequence=2 if self.usage_before_error else 1,
                    category="connection",
                    message="not connected",
                    retryable=True,
                )
            return
        yield ModelTextDelta(sequence=1, content="recovered")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)


async def test_transient_provider_failure_retries_only_before_visible_output() -> None:
    driver = _FlakyDriver()
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(driver)
    outcome = await loop.run(_request(1), emit=emit)

    assert outcome.output == "recovered"
    assert driver.calls == 2
    assert sum(isinstance(event, LoopRetryScheduled) for event in events) == 1


async def test_midstream_failure_retracts_candidate_and_recovers_without_replaying_tools() -> None:
    driver = _FlakyDriver(fail_after_text=True)
    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(
        driver, limits=LoopLimits(model_retry_delay_seconds=0),
    )
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    outcome = await loop.run(_request(1), emit=emit)
    assert outcome.output == "recovered"
    assert driver.calls == outcome.model_attempts == 2
    assert outcome.request_count == 1
    assert [e.characters for e in events if isinstance(e, LoopTextRetracted)] == [len("partial")]
    assert loop.state is LoopState.COMPLETED


@pytest.mark.parametrize("active_stream", [True, False])
async def test_request_deadline_cancels_even_an_active_thinking_stream(active_stream: bool) -> None:
    closed = asyncio.Event()
    calls = 0

    class EndlessDriver(_StreamDriverMixin):
        async def stream(
            self, request: ModelDriverRequest[dict[str, Any]],
        ) -> AsyncIterator[ModelStreamEvent]:
            nonlocal calls
            calls += 1
            try:
                yield ModelResponseStarted(sequence=0)
                yield ModelToolCallStarted(sequence=1, call_id="pending", name="write_file")
                yield ModelToolCallCompleted(
                    sequence=2, call_id="pending", name="write_file", arguments={"path": "never"},
                )
                sequence = 3
                while True:
                    await asyncio.sleep(0.001)
                    if active_stream:
                        yield ModelThinkingDelta(sequence=sequence, content="still thinking")
                        sequence += 1
            finally:
                closed.set()

    executed = False

    async def continue_tools(
        value: LoopToolContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        nonlocal executed
        executed = True
        return value.prior_request

    loop = LumenAgentLoop[dict[str, Any]](
        EndlessDriver(), limits=LoopLimits(model_request_timeout_seconds=0.03),
    )
    with pytest.raises(LoopRequestTimeout, match="model request deadline"):
        await loop.run(_request(1), continue_after_tools=continue_tools)
    assert closed.is_set()
    assert calls == 1
    assert not executed
    assert loop.state is LoopState.FAILED


async def test_idle_deadline_slides_across_long_active_generation() -> None:
    class SlowDriver(_StreamDriverMixin):
        async def stream(
            self, request: ModelDriverRequest[dict[str, Any]],
        ) -> AsyncIterator[ModelStreamEvent]:
            yield ModelResponseStarted(sequence=0)
            for index in range(1, 9):
                await asyncio.sleep(0.02)
                yield ModelThinkingDelta(sequence=index, content="thinking")
            yield ModelTextDelta(sequence=9, content="done")
            yield ModelResponseCompleted(sequence=10, stop_reason=ModelStopReason.END_TURN)

    loop = LumenAgentLoop(SlowDriver(), limits=LoopLimits(model_stream_idle_timeout_seconds=0.1))
    result = await loop.run(_request(1))
    assert result.output == "done"
    assert result.model_attempts == 1


async def test_idle_retry_is_bounded_and_closes_each_stream() -> None:
    closed: list[int] = []

    class SilentDriver(_StreamDriverMixin):
        async def stream(
            self, request: ModelDriverRequest[dict[str, Any]],
        ) -> AsyncIterator[ModelStreamEvent]:
            try:
                yield ModelResponseStarted(sequence=0)
                await asyncio.Event().wait()
            finally:
                closed.append(1)

    loop = LumenAgentLoop(SilentDriver(), limits=LoopLimits(
        model_stream_idle_timeout_seconds=0.02, model_retries=2, model_retry_delay_seconds=0,
    ))
    with pytest.raises(LoopProviderFailure, match="no activity") as captured:
        await loop.run(_request(1))
    assert len(closed) == captured.value.model_attempts == 3
    assert captured.value.request_count == 1


async def test_cancelling_backoff_does_not_start_another_request() -> None:
    retrying = asyncio.Event()
    driver = _FlakyDriver()
    loop = LumenAgentLoop(driver, limits=LoopLimits(model_retry_delay_seconds=30))

    async def emit(event: LoopEvent) -> None:
        if isinstance(event, LoopRetryScheduled):
            retrying.set()

    task = asyncio.create_task(loop.run(_request(1), emit=emit))
    await asyncio.wait_for(retrying.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.calls == 1
    assert loop.state is LoopState.CANCELLED


@pytest.mark.parametrize("replay_safe,retry_after", [(False, None), (True, 3600)])
async def test_unknown_effect_and_long_retry_after_are_not_automatically_replayed(
    replay_safe: bool, retry_after: float | None,
) -> None:
    class UnsafeDriver(_StreamDriverMixin):
        calls = 0

        async def stream(
            self, request: ModelDriverRequest[dict[str, Any]],
        ) -> AsyncIterator[ModelStreamEvent]:
            self.calls += 1
            yield ModelResponseStarted(sequence=0)
            yield ModelProviderError(
                sequence=1, category="http_429", message="unavailable", retryable=True,
                replay_safe=replay_safe, retry_after_seconds=retry_after,
            )

    driver = UnsafeDriver()
    loop = LumenAgentLoop(driver, limits=LoopLimits(model_retry_delay_seconds=0))
    with pytest.raises(LoopProviderFailure):
        await loop.run(_request(1))
    assert driver.calls == 1


async def test_default_loop_completes_more_than_60_requests_and_100_tools(tmp_path: Path) -> None:
    observed: list[int] = []

    async def observe(value: int) -> str:
        observed.append(value)
        return str(value)

    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([
        *(_tool_recording(i, (f"c{i}", "observe", {"value": i})) for i in range(1, 106)),
        _recording(106, "finished"),
    ])

    async def continue_tools(
        value: LoopToolContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        return _request(value.request_index + 1)

    loop = LumenAgentLoop(driver, capability_gateway=_gateway(tmp_path, ToolSpec(observe, risk=Risk.READ)))
    outcome = await loop.run(_request(1), execution_id="long", continue_after_tools=continue_tools)
    assert observed == list(range(1, 106))
    assert outcome.tool_call_count == 105
    assert outcome.request_count == outcome.model_attempts == 106
    assert outcome.output == "finished"


@pytest.mark.parametrize("fails_twice", [False, True])
async def test_context_overflow_changes_the_request_and_has_only_one_recovery(fails_twice: bool) -> None:
    class OverflowDriver(_StreamDriverMixin):
        calls = 0

        async def stream(
            self, request: ModelDriverRequest[dict[str, Any]],
        ) -> AsyncIterator[ModelStreamEvent]:
            self.calls += 1
            yield ModelResponseStarted(sequence=0)
            yield ModelUsage(sequence=1, input_tokens=3)
            if self.calls == 1 or fails_twice:
                yield ModelProviderError(
                    sequence=2, category="context_overflow", message="too long",
                )
            else:
                assert request.request_id == "request-2"
                yield ModelTextDelta(sequence=2, content="done")
                yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.END_TURN)

    compactions = 0

    async def compact(
        request: ModelDriverRequest[dict[str, Any]], step: int,
    ) -> ModelDriverRequest[dict[str, Any]]:
        nonlocal compactions
        compactions += 1
        return _request(step)

    driver = OverflowDriver()
    loop = LumenAgentLoop(driver)
    if fails_twice:
        with pytest.raises(LoopContextOverflow) as captured:
            await loop.run(_request(1), compact_request=compact)
        assert captured.value.usage.input_tokens == 6
    else:
        result = await loop.run(_request(1), compact_request=compact)
        assert result.output == "done"
        assert result.usage.input_tokens == 6
    assert driver.calls == 2
    assert compactions == 1


async def test_usage_only_activity_does_not_disable_safe_provider_retry() -> None:
    driver = _FlakyDriver(usage_before_error=True)

    outcome = await LumenAgentLoop[dict[str, Any]](driver).run(_request(1))

    assert outcome.output == "recovered"
    assert driver.calls == 2
    assert outcome.usage.input_tokens == 7


class _InvalidSequenceDriver(_StreamDriverMixin):
    async def stream(self, request: ModelDriverRequest[dict[str, Any]]) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelResponseStarted(sequence=0)
        yield ModelTextDelta(sequence=2, content="skipped one")


async def test_provider_event_sequence_mismatch_fails_closed() -> None:
    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(_InvalidSequenceDriver())

    with pytest.raises(LoopProtocolError, match="expected 1, received 2"):
        await loop.run(_request(1))

    assert loop.state is LoopState.FAILED


async def test_length_stop_and_tool_calls_never_reach_completion_gate() -> None:
    length_recording = ReplayRecording(
        request_fingerprint=_manifest(1).request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0),
            ModelTextDelta(sequence=1, content="cut off"),
            ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.LENGTH),
        ),
    )
    tool_recording = ReplayRecording(
        request_fingerprint=_manifest(2, step=1).request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0),
            ModelToolCallStarted(sequence=1, call_id="call-one", name="read_file"),
            ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.TOOL_CALL),
        ),
    )

    length_driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([length_recording])
    tool_driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([tool_recording])
    with pytest.raises(LoopTruncated, match="output limit"):
        await LumenAgentLoop[dict[str, Any]](length_driver).run(_request(1))
    with pytest.raises(LoopTruncated, match="incomplete tool calls"):
        await LumenAgentLoop[dict[str, Any]](tool_driver).run(_request(2, step=1))

    complete_tool_recording = ReplayRecording(
        request_fingerprint=_manifest(2, step=1).request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0),
            ModelToolCallStarted(sequence=1, call_id="call-one", name="read_file"),
            # Complete calls still fail closed when no CapabilityGateway is attached.
            ModelToolCallCompleted(
                sequence=2,
                call_id="call-one",
                name="read_file",
                arguments={"path": "README.md"},
            ),
            ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.TOOL_CALL),
        ),
    )
    complete_tool_driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([complete_tool_recording])
    with pytest.raises(LoopToolCallsUnsupported, match="CapabilityGateway"):
        await LumenAgentLoop[dict[str, Any]](complete_tool_driver).run(_request(2, step=1))


async def test_incomplete_tool_call_can_retry_at_a_safe_request_boundary() -> None:
    class LengthThenTextDriver(_StreamDriverMixin):
        calls = 0

        async def stream(
            self, request: ModelDriverRequest[dict[str, Any]]
        ) -> AsyncIterator[ModelStreamEvent]:
            del request
            self.calls += 1
            yield ModelResponseStarted(sequence=0)
            if self.calls == 1:
                yield ModelToolCallStarted(sequence=1, call_id="partial", name="write_file")
                yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.LENGTH)
                return
            yield ModelTextDelta(sequence=1, content="recovered")
            yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)

    driver = LengthThenTextDriver()
    seen: list[LoopTruncationContinuation[dict[str, Any]]] = []

    def continue_truncated(
        continuation: LoopTruncationContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        seen.append(continuation)
        return _request(2)

    outcome = await LumenAgentLoop[dict[str, Any]](driver).run(
        _request(1),
        continue_truncated=continue_truncated,
    )

    assert outcome.output == "recovered"
    assert driver.calls == 2
    assert seen[0].incomplete_tool_calls == ("partial",)
    assert any(
        transition.previous is LoopState.COLLECTING_TOOL_CALLS
        and transition.current is LoopState.REQUESTING_MODEL
        for transition in outcome.transitions
    )


@pytest.mark.parametrize(
    "stop_reason",
    [
        ModelStopReason.CONTENT_FILTER,
        ModelStopReason.REFUSAL,
        ModelStopReason.ERROR,
        ModelStopReason.UNKNOWN,
    ],
)
async def test_non_success_stop_reasons_fail_closed(stop_reason: ModelStopReason) -> None:
    recording = ReplayRecording(
        request_fingerprint=_manifest(1).request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0),
            ModelResponseCompleted(sequence=1, stop_reason=stop_reason),
        ),
    )

    with pytest.raises(LoopProviderFailure, match=stop_reason.value):
        await LumenAgentLoop[dict[str, Any]](ReplayModelDriver([recording])).run(_request(1))


async def test_filtered_response_never_executes_a_completed_tool_call(tmp_path: Path) -> None:
    calls = 0

    async def mutate(value: str) -> str:
        nonlocal calls
        calls += 1
        return value

    recording = ReplayRecording(
        request_fingerprint=_manifest(1).request_fingerprint,
        events=(
            ModelResponseStarted(sequence=0),
            ModelToolCallStarted(sequence=1, call_id="write-1", name="mutate"),
            ModelToolCallCompleted(
                sequence=2,
                call_id="write-1",
                name="mutate",
                arguments={"value": "unsafe"},
            ),
            ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.CONTENT_FILTER),
        ),
    )
    gateway = _gateway(tmp_path, ToolSpec(mutate, risk=Risk.WRITE))

    with pytest.raises(LoopProviderFailure, match="incompatible stop reason"):
        await LumenAgentLoop[dict[str, Any]](
            ReplayModelDriver([recording]),
            capability_gateway=gateway,
        ).run(
            _request(1),
            continue_after_tools=lambda _continuation: _request(2),
            execution_id="filtered-tool-run",
        )

    assert calls == 0


async def test_tool_denial_is_returned_to_the_model_without_execution(tmp_path: Path) -> None:
    calls = 0

    async def mutate(value: str) -> str:
        nonlocal calls
        calls += 1
        return value

    gateway = _gateway(tmp_path, ToolSpec(mutate, risk=Risk.WRITE))
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver(
        [
            _tool_recording(1, ("write-1", "mutate", {"value": "unsafe"}), text="trying"),
            _recording(2, "I will not change it."),
        ]
    )
    observed_results: list[CapabilityStatus] = []
    events: list[LoopEvent] = []

    async def approve(_request: ApprovalRequest) -> CapabilityApproval:
        return CapabilityApproval(False, "denied by test")

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    async def continue_after_tools(
        continuation: LoopToolContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        observed_results.extend(result.status for result in continuation.results)
        assert continuation.results[0].error == "denied by test"
        return _request(2)

    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(
        driver,
        capability_gateway=gateway,
    )
    outcome = await loop.run(
        _request(1),
        emit=emit,
        continue_after_tools=continue_after_tools,
        execution_id="denial-run",
        approve=approve,
    )

    assert outcome.output == "I will not change it."
    assert outcome.tool_call_count == 1
    assert calls == 0
    assert observed_results == [CapabilityStatus.DENIED]
    assert any(isinstance(event, LoopToolResultRecorded) and event.status == "denied" for event in events)
    assert [transition.current for transition in outcome.transitions][2:6] == [
        LoopState.COLLECTING_TOOL_CALLS,
        LoopState.AWAITING_APPROVAL,
        LoopState.EXECUTING_TOOLS,
        LoopState.APPENDING_TOOL_RESULTS,
    ]


async def test_parallel_safe_calls_overlap_but_exclusive_calls_are_barriers(tmp_path: Path) -> None:
    active = 0
    safe_pair_started = asyncio.Event()
    trailing_pair_started = asyncio.Event()
    log: list[str] = []

    async def scheduled(label: str, safe: bool) -> str:
        nonlocal active
        active += 1
        log.append(f"start:{label}")
        if label in {"a", "b"} and active == 2:
            safe_pair_started.set()
        if label in {"c", "d"} and active == 2:
            trailing_pair_started.set()
        if safe:
            await asyncio.wait_for(
                safe_pair_started.wait() if label in {"a", "b"} else trailing_pair_started.wait(),
                timeout=0.5,
            )
        else:
            assert active == 1
        active -= 1
        log.append(f"end:{label}")
        return label

    gateway = _gateway(
        tmp_path,
        ToolSpec(
            scheduled,
            risk=Risk.READ,
            concurrency=lambda arguments: (
                ToolConcurrency.PARALLEL_SAFE if arguments["safe"] is True else ToolConcurrency.EXCLUSIVE
            ),
        ),
    )
    calls = (
        ("call-a", "scheduled", {"label": "a", "safe": True}),
        ("call-b", "scheduled", {"label": "b", "safe": True}),
        ("call-x", "scheduled", {"label": "x", "safe": False}),
        ("call-c", "scheduled", {"label": "c", "safe": True}),
        ("call-d", "scheduled", {"label": "d", "safe": True}),
    )
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver(
        [_tool_recording(1, *calls), _recording(2, "done")]
    )

    async def continue_after_tools(
        continuation: LoopToolContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        assert [result.output for result in continuation.results] == ["a", "b", "x", "c", "d"]
        return _request(2)

    outcome = await LumenAgentLoop[dict[str, Any]](
        driver,
        capability_gateway=gateway,
        limits=LoopLimits(parallel_tool_calls=True),
    ).run(
        _request(1),
        continue_after_tools=continue_after_tools,
        execution_id="parallel-run",
    )

    assert outcome.tool_call_count == 5
    assert safe_pair_started.is_set()
    assert trailing_pair_started.is_set()
    assert log.index("start:x") > log.index("end:b")
    assert log.index("start:c") > log.index("end:x")


async def test_repeated_tool_trajectory_emits_stall_evidence(tmp_path: Path) -> None:
    async def inspect(value: str) -> str:
        return value

    gateway = _gateway(tmp_path, ToolSpec(inspect, risk=Risk.READ))
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver(
        [
            _tool_recording(1, ("inspect-1", "inspect", {"value": "same"})),
            _tool_recording(2, ("inspect-2", "inspect", {"value": "same"})),
            _recording(3, "finished"),
        ]
    )
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    async def continue_after_tools(
        continuation: LoopToolContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        return _request(continuation.request_index + 1)

    outcome = await LumenAgentLoop[dict[str, Any]](
        driver,
        capability_gateway=gateway,
    ).run(
        _request(1),
        emit=emit,
        continue_after_tools=continue_after_tools,
        execution_id="stall-run",
    )

    assert outcome.output == "finished"
    stall = next(event for event in events if isinstance(event, LoopStallObserved))
    assert stall.call_id == "inspect-2"
    assert stall.window_count == 2


async def test_cancellation_during_tool_execution_requires_reconciliation(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def block() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    gateway = _gateway(tmp_path, ToolSpec(block, risk=Risk.READ))
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver(
        [_tool_recording(1, ("block-1", "block", {}))]
    )

    async def unreachable_continuation(
        _continuation: LoopToolContinuation[dict[str, Any]],
    ) -> ModelDriverRequest[dict[str, Any]]:
        raise AssertionError("cancelled tool calls must not continue the model")

    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(
        driver,
        capability_gateway=gateway,
    )
    task = asyncio.create_task(
        loop.run(
            _request(1),
            continue_after_tools=unreachable_continuation,
            execution_id="cancel-tool-run",
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.state is LoopState.RECONCILIATION_REQUIRED


async def test_cancellation_publishes_receipts_for_parallel_calls_that_already_finished(
    tmp_path: Path,
) -> None:
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def fast() -> str:
        return "finished"

    async def slow() -> str:
        blocked.set()
        await release.wait()
        return "late"

    def parallel(_arguments: dict[str, Any]) -> ToolConcurrency:
        return ToolConcurrency.PARALLEL_SAFE
    gateway = _gateway(
        tmp_path,
        ToolSpec(fast, risk=Risk.READ, concurrency=parallel),
        ToolSpec(slow, risk=Risk.READ, concurrency=parallel),
    )
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver(
        [_tool_recording(1, ("fast-1", "fast", {}), ("slow-1", "slow", {}))]
    )
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    loop = LumenAgentLoop[dict[str, Any]](driver, capability_gateway=gateway)
    task = asyncio.create_task(
        loop.run(
            _request(1),
            emit=emit,
            continue_after_tools=lambda _continuation: _request(2),
            execution_id="parallel-cancel-run",
        )
    )
    await blocked.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    finished = [
        event.call_id for event in events if isinstance(event, LoopToolResultRecorded)
    ]
    assert finished == ["fast-1"]
    assert loop.state is LoopState.RECONCILIATION_REQUIRED


@pytest.mark.parametrize("parallel", [True, False])
async def test_tool_completion_is_visible_before_the_next_result_and_history_stays_ordered(
    tmp_path: Path, parallel: bool,
) -> None:
    fast_visible = asyncio.Event()
    events: list[LoopEvent] = []

    async def fast() -> str:
        return "fast"

    async def slow() -> str:
        # This cannot finish until the consumer has seen the fast result.
        # A scheduler that waits for the entire batch deadlocks here.
        await asyncio.wait_for(fast_visible.wait(), timeout=0.5)
        return "slow"

    gateway = _gateway(tmp_path, *(
        ToolSpec(function, risk=Risk.READ, concurrency=lambda _: ToolConcurrency.PARALLEL_SAFE)
        for function in (fast, slow)
    ))
    names = ("slow", "fast") if parallel else ("fast", "slow")
    calls: list[tuple[str, str, dict[str, Any]]] = [(name, name, {}) for name in names]
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([
        _tool_recording(1, *calls), _recording(2, "done"),
    ])

    async def emit(event: LoopEvent) -> None:
        await asyncio.sleep(0)  # Deliberately asynchronous event consumer.
        events.append(event)
        if isinstance(event, LoopToolResultRecorded) and event.call_id == "fast":
            fast_visible.set()

    def continuation(step: LoopToolContinuation[dict[str, Any]]) -> ModelDriverRequest[dict[str, Any]]:
        assert [result.output for result in step.results] == list(names)
        assert all(result.succeeded for result in step.results)
        return _request(2)

    outcome = await LumenAgentLoop(
        driver, capability_gateway=gateway, limits=LoopLimits(parallel_tool_calls=parallel),
    ).run(_request(1), emit=emit, continue_after_tools=continuation, execution_id="early-results")
    results = [e for e in events if isinstance(e, LoopToolResultRecorded)]
    assert [e.name for e in results] == ["fast", "slow"]
    assert [e.order for e in results] == ([1, 0] if parallel else [0, 1])
    assert [e.sequence for e in events] == list(range(len(events)))
    assert all(e.execution_seconds is not None and e.elapsed_seconds >= e.execution_seconds for e in results)
    assert outcome.tool_call_count == 2


async def test_cancellation_finishes_inflight_result_publication_once(tmp_path: Path) -> None:
    publishing = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []
    events: list[LoopEvent] = []

    async def inspect(label: str) -> str:
        calls.append(label)
        return label

    gateway = _gateway(tmp_path, ToolSpec(inspect, risk=Risk.READ))
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([
        _tool_recording(1, ("first", "inspect", {"label": "first"}),
                        ("second", "inspect", {"label": "second"})),
    ])

    async def emit(event: LoopEvent) -> None:
        events.append(event)
        if isinstance(event, LoopToolResultRecorded):
            publishing.set()
            await release.wait()

    loop = LumenAgentLoop(driver, capability_gateway=gateway)
    task = asyncio.create_task(loop.run(
        _request(1), emit=emit, continue_after_tools=lambda _: _request(2), execution_id="cancel-publication",
    ))
    await asyncio.wait_for(publishing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["first"]
    assert [e.call_id for e in events if isinstance(e, LoopToolResultRecorded)] == ["first"]
    assert [e.sequence for e in events] == list(range(len(events)))
    assert loop.state is LoopState.RECONCILIATION_REQUIRED


async def test_request_observations_distinguish_retry_and_success_without_storing_text() -> None:
    driver = _FlakyDriver(fail_after_text=True)
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    await LumenAgentLoop[dict[str, Any]](
        driver, limits=LoopLimits(model_retry_delay_seconds=0),
    ).run(_request(1), emit=emit)
    observations = [e for e in events if isinstance(e, LoopRequestObserved)]
    assert len(observations) == 2
    assert [e.request_index for e in observations] == [1, 1]
    assert [e.model_attempts for e in observations] == [1, 2]
    assert observations[0].error_category is not None
    assert observations[1].stop_reason is ModelStopReason.END_TURN
    assert all(e.first_text_seconds is not None and e.text_characters > 0 for e in observations)
    assert all(e.first_thinking_seconds is None for e in observations)
    assert all("content" not in e.model_dump() for e in observations)


class _BlockingDriver(_StreamDriverMixin):
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, request: ModelDriverRequest[dict[str, Any]]) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelResponseStarted(sequence=0)
        self.started.set()
        await asyncio.Event().wait()
        yield ModelResponseCompleted(sequence=1, stop_reason=ModelStopReason.END_TURN)


class _SuspendedDriver(_StreamDriverMixin):
    def __init__(self) -> None:
        self.calls = 0
        self.merges = 0

    @asynccontextmanager
    async def open_stream(
        self, request: ModelDriverRequest[dict[str, Any]]
    ) -> AsyncGenerator[_TestDriverStream, None]:
        del request
        self.calls += 1
        if self.calls == 1:
            events = iter_stream(
                ModelResponseStarted(sequence=0, provider_response_id="job-one"),
                ModelTextDelta(sequence=1, content="working"),
                ModelResponseCompleted(
                    sequence=2,
                    stop_reason=ModelStopReason.SUSPENDED,
                    provider_response_id="job-one",
                ),
            )
            stream = _TestDriverStream(events)
            stream.response = {"id": "job-one", "content": "working", "state": "suspended"}
        else:
            events = iter_stream(
                ModelResponseStarted(sequence=0, provider_response_id="job-one"),
                ModelTextDelta(sequence=1, content="done"),
                ModelResponseCompleted(
                    sequence=2,
                    stop_reason=ModelStopReason.END_TURN,
                    provider_response_id="job-one",
                ),
            )
            stream = _TestDriverStream(events)
            stream.response = {"id": "job-one", "content": "done", "state": "complete"}
        yield stream

    def merge_responses(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        assert previous["id"] == current["id"]
        self.merges += 1
        return current


async def iter_stream(*events: ModelStreamEvent) -> AsyncIterator[ModelStreamEvent]:
    for event in events:
        yield event


async def test_suspended_response_polls_and_merges_exact_response() -> None:
    driver = _SuspendedDriver()
    continuations: list[dict[str, Any]] = []

    async def continue_suspended(continuation: Any) -> ModelDriverRequest[dict[str, Any]]:
        continuations.append(continuation.response)
        return _request(2)

    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(driver)
    outcome = await loop.run(_request(1), continue_suspended=continue_suspended)

    assert outcome.output == "done"
    assert outcome.response == {"id": "job-one", "content": "done", "state": "complete"}
    assert continuations == [{"id": "job-one", "content": "working", "state": "suspended"}]
    assert driver.calls == 2
    assert driver.merges == 1


async def test_cancellation_has_a_terminal_state_transition() -> None:
    driver = _BlockingDriver()
    loop: LumenAgentLoop[dict[str, Any]] = LumenAgentLoop(driver)
    events: list[LoopEvent] = []

    async def emit(event: LoopEvent) -> None:
        events.append(event)

    task = asyncio.create_task(loop.run(_request(1), emit=emit))
    await driver.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.state is LoopState.CANCELLED
    transition = next(event for event in reversed(events) if isinstance(event, LoopTransition))
    assert transition.current is LoopState.CANCELLED


def test_illegal_loop_transition_is_rejected() -> None:
    with pytest.raises(LoopProtocolError, match="preparing -> completed"):
        LumenAgentLoop.validate_transition(LoopState.PREPARING, LoopState.COMPLETED)
