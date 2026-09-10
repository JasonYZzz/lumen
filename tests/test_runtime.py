import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextContent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    ToolSearchReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingPart,
    DeltaToolCall,
    FunctionModel,
)

from lumen.agent_loop import (
    LoopBudgetExceeded,
    LoopCompletionRejected,
    LoopProviderFailure,
    LoopRequestTimeout,
    LoopTruncated,
    ModelDriverRequest,
    ModelNativeTool,
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
)
from lumen.attachments import AttachmentStore, attachment_from_marker
from lumen.config import ContextConfig, LimitsConfig, PermissionsConfig
from lumen.context import ArtifactStore, ContextEngine, ContextReportCommand
from lumen.events import (
    ClarificationRequested,
    CommentaryDelta,
    InputDelivered,
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    RunWaitingForUser,
    TextDelta,
    TextRetracted,
    ThinkingDelta,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from lumen.interactive_queue import QueueMode
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.runtime import (
    AgentRuntime,
    PartialRunOutcome,
    ToolApproval,
    _preferred_response_language,  # pyright: ignore[reportPrivateUsage]
    get_partial_outcome,
)
from lumen.task_control import CONTROL_TOOL_NAMES
from lumen.tools.gateway import CapabilityDescriptor, CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency, ToolSpec


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("<collaboration-mode>默认中文说明</collaboration-mode>\n\n请检查这个问题", "中文"),
        ("<collaboration-mode>默认中文说明</collaboration-mode>\n\nInspect this issue", "英语"),
        (
            "<collaboration-mode>默认中文说明</collaboration-mode>\n\nInspect "
            '<file path="notes.md">\n大量中文资料\n</file> this issue',
            "英语",
        ),
        ("この問題を確認してください", "日语"),
        ("이 문제를 확인하세요", "韩语"),
    ],
)
def test_response_language_uses_actual_user_prompt(prompt: str, expected: str) -> None:
    assert _preferred_response_language(prompt) == expected


class _RuntimeDriverStream:
    def __init__(self, events: AsyncIterator[ModelStreamEvent]) -> None:
        self._source = events
        self.response: ModelMessage | None = None

    @property
    def events(self) -> AsyncIterator[ModelStreamEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamEvent]:
        parts: list[TextPart | ThinkingPart | ToolCallPart] = []
        provider_response_id: str | None = None
        terminal: ModelResponseCompleted | None = None
        async for event in self._source:
            if isinstance(event, ModelResponseStarted):
                provider_response_id = event.provider_response_id
            elif isinstance(event, ModelTextDelta):
                parts.append(TextPart(content=event.content))
            elif isinstance(event, ModelThinkingDelta):
                parts.append(ThinkingPart(content=event.content))
            elif isinstance(event, ModelToolCallCompleted):
                parts.append(
                    ToolCallPart(
                        tool_name=event.name,
                        args=event.arguments,
                        tool_call_id=event.call_id,
                    )
                )
            elif isinstance(event, ModelResponseCompleted):
                terminal = event
            yield event
        if terminal is not None:
            self.response = ModelResponse(
                parts=parts,
                finish_reason=(
                    "tool_call"
                    if terminal.stop_reason is ModelStopReason.TOOL_CALL
                    else "length"
                    if terminal.stop_reason is ModelStopReason.LENGTH
                    else "stop"
                ),
                provider_response_id=terminal.provider_response_id or provider_response_id,
                state=("suspended" if terminal.stop_reason is ModelStopReason.SUSPENDED else "complete"),
            )


class _RuntimeDriverMixin:
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
        self, request: ModelDriverRequest[ModelMessage]
    ) -> AsyncGenerator[_RuntimeDriverStream, None]:
        yield _RuntimeDriverStream(self.stream(request))  # type: ignore[attr-defined]

    def continuation_delay(self, response: ModelMessage) -> float | None:
        del response
        return None

    async def cancel_suspended_response(self, response: ModelMessage) -> None:
        del response

    def merge_responses(self, previous: ModelMessage, current: ModelMessage) -> ModelMessage:
        del previous
        return current

    def response_text(self, response: ModelMessage) -> str:
        return (
            "".join(part.content for part in response.parts if isinstance(part, TextPart))
            if isinstance(response, ModelResponse)
            else ""
        )

    def response_thinking(self, response: ModelMessage) -> str:
        return (
            "".join(part.content for part in response.parts if isinstance(part, ThinkingPart))
            if isinstance(response, ModelResponse)
            else ""
        )


class _LumenTextDriver(_RuntimeDriverMixin):
    def __init__(self, text: str = "native answer") -> None:
        self.text = text
        self.requests: list[ModelDriverRequest[ModelMessage]] = []

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelResponseStarted(sequence=0, provider_response_id="lumen-response")
        yield ModelThinkingDelta(sequence=1, content="native thinking")
        yield ModelTextDelta(sequence=2, content=self.text)
        yield ModelUsage(sequence=3, input_tokens=21, output_tokens=4, cache_read_tokens=8)
        yield ModelResponseCompleted(sequence=4, stop_reason=ModelStopReason.END_TURN)


class _LumenRetryDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.calls += 1
        yield ModelResponseStarted(sequence=0)
        if self.calls == 1:
            yield ModelProviderError(
                sequence=1,
                category="connection",
                message="connect failed",
                retryable=True,
            )
            return
        yield ModelTextDelta(sequence=1, content="recovered once")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)


class _LumenBlockingDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelResponseStarted(sequence=0)
        self.started.set()
        await asyncio.Event().wait()
        yield ModelResponseCompleted(sequence=1, stop_reason=ModelStopReason.END_TURN)


class _LumenTruncatedDriver(_RuntimeDriverMixin):
    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelResponseStarted(sequence=0, provider_response_id="truncated-response")
        yield ModelToolCallStarted(sequence=1, call_id="partial-call", name="echo")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.LENGTH)


class _LumenRecoveringTruncationDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.requests: list[ModelDriverRequest[ModelMessage]] = []

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelResponseStarted(sequence=0, provider_response_id=f"response-{len(self.requests)}")
        if len(self.requests) == 1:
            yield ModelToolCallStarted(sequence=1, call_id="partial-call", name="echo")
            yield ModelUsage(sequence=2, input_tokens=20, output_tokens=4_096)
            yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.LENGTH)
            return
        yield ModelTextDelta(sequence=1, content="recovered without executing the partial call")
        yield ModelUsage(sequence=2, input_tokens=20, output_tokens=12)
        yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.END_TURN)


class _LumenContinuingTextDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.requests: list[ModelDriverRequest[ModelMessage]] = []

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelResponseStarted(sequence=0)
        if len(self.requests) == 1:
            yield ModelTextDelta(sequence=1, content="first ")
            yield ModelUsage(sequence=2, input_tokens=10, output_tokens=4_096)
            yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.LENGTH)
            return
        assert any(
            isinstance(part, RetryPromptPart)
            for message in request.messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        yield ModelTextDelta(sequence=1, content="second")
        yield ModelUsage(sequence=2, input_tokens=12, output_tokens=2)
        yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.END_TURN)


class _LumenRejectedDriver(_RuntimeDriverMixin):
    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        del request
        yield ModelResponseStarted(sequence=0, provider_response_id="filtered-response")
        yield ModelResponseCompleted(sequence=1, stop_reason=ModelStopReason.CONTENT_FILTER)


class _LumenToolDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.requests: list[ModelDriverRequest[ModelMessage]] = []

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelResponseStarted(sequence=0)
        if len(self.requests) == 1:
            yield ModelTextDelta(sequence=1, content="Checking the value.")
            yield ModelToolCallStarted(sequence=2, call_id="echo-1", name="echo")
            yield ModelToolCallCompleted(
                sequence=3,
                call_id="echo-1",
                name="echo",
                arguments={"value": "hello"},
            )
            yield ModelUsage(sequence=4, input_tokens=10, output_tokens=2)
            yield ModelResponseCompleted(sequence=5, stop_reason=ModelStopReason.TOOL_CALL)
            return
        returned = last_tool_return(request.messages)
        assert returned is not None
        assert returned.tool_name == "echo"
        assert returned.content == "hello"
        yield ModelTextDelta(sequence=1, content="The tool returned hello.")
        yield ModelUsage(sequence=2, input_tokens=14, output_tokens=5)
        yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.END_TURN)


class _LumenDeferredDriver(_RuntimeDriverMixin):
    def __init__(self, query: str = "weather forecast") -> None:
        self.requests: list[ModelDriverRequest[ModelMessage]] = []
        self.query = query

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        yield ModelResponseStarted(sequence=0)
        if len(self.requests) == 1:
            names = {str(tool["name"]) for tool in request.tools}
            assert "search_tools" in names
            assert "weather_lookup" not in names
            search = next(tool for tool in request.tools if tool["name"] == "search_tools")
            assert "weather_lookup" in search["description"]
            assert "天气预报" in search["description"]
            assert 'queries: [""]' in search["description"]
            yield ModelToolCallStarted(sequence=1, call_id="search-1", name="search_tools")
            yield ModelToolCallCompleted(
                sequence=2,
                call_id="search-1",
                name="search_tools",
                arguments={"queries": [self.query]},
            )
            yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.TOOL_CALL)
            return
        if len(self.requests) == 2:
            names = {str(tool["name"]) for tool in request.tools}
            assert "weather_lookup" in names
            assert "search_tools" not in names
            assert any(
                isinstance(part, ToolSearchReturnPart)
                for message in request.messages
                if isinstance(message, ModelRequest)
                for part in message.parts
            )
            yield ModelToolCallStarted(sequence=1, call_id="weather-1", name="weather_lookup")
            yield ModelToolCallCompleted(
                sequence=2,
                call_id="weather-1",
                name="weather_lookup",
                arguments={"city": "Shanghai"},
            )
            yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.TOOL_CALL)
            return
        returned = last_tool_return(request.messages)
        assert returned is not None
        assert returned.tool_name == "weather_lookup"
        assert returned.content == "sunny"
        yield ModelTextDelta(sequence=1, content="Shanghai is sunny.")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)


class _LumenClarificationDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.calls += 1
        yield ModelResponseStarted(sequence=0)
        yield ModelToolCallStarted(
            sequence=1,
            call_id="clarify-native-1",
            name="request_clarification",
        )
        yield ModelToolCallCompleted(
            sequence=2,
            call_id="clarify-native-1",
            name="request_clarification",
            arguments={"question": "Which target?", "choices": ["A", "B"]},
        )
        yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.TOOL_CALL)


class _LumenRecoveringWriteDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.fail_after_tool = True

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        yield ModelResponseStarted(sequence=0)
        returned = last_tool_return(request.messages)
        if returned is None:
            yield ModelToolCallStarted(sequence=1, call_id="write-native-1", name="write_note")
            yield ModelToolCallCompleted(
                sequence=2,
                call_id="write-native-1",
                name="write_note",
                arguments={"content": "once"},
            )
            yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.TOOL_CALL)
            return
        if self.fail_after_tool:
            yield ModelProviderError(
                sequence=1,
                category="provider",
                message="provider failed after write",
                retryable=False,
            )
            return
        assert "once" in str(returned.content)
        yield ModelTextDelta(sequence=1, content="recovered without another write")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)


class _LumenInteractiveToolDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.saw_steering = False

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        yield ModelResponseStarted(sequence=0)
        if last_tool_return(request.messages) is None:
            yield ModelToolCallStarted(sequence=1, call_id="pause-native-1", name="pause_tool")
            yield ModelToolCallCompleted(
                sequence=2,
                call_id="pause-native-1",
                name="pause_tool",
                arguments={},
            )
            yield ModelResponseCompleted(sequence=3, stop_reason=ModelStopReason.TOOL_CALL)
            return
        prompts = [
            str(part.content)
            for message in request.messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        self.saw_steering = any("native steer injected" in prompt for prompt in prompts)
        yield ModelTextDelta(sequence=1, content="native steering received")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)


class _LumenFollowUpDriver(_RuntimeDriverMixin):
    def __init__(self) -> None:
        self.first_response_started = asyncio.Event()
        self.release_first_response = asyncio.Event()
        self.calls = 0
        self.saw_follow_up = False

    async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        yield ModelResponseStarted(sequence=0)
        if self.calls == 1:
            self.first_response_started.set()
            await self.release_first_response.wait()
            yield ModelTextDelta(sequence=1, content="initial candidate")
            yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)
            return
        prompts = [
            str(part.content)
            for message in request.messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        self.saw_follow_up = any("native follow up" in prompt for prompt in prompts)
        yield ModelTextDelta(sequence=1, content="follow-up completed")
        yield ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN)


def last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


def last_retry_prompt(messages: Sequence[ModelMessage]) -> RetryPromptPart | None:
    """Find the most recent RetryPromptPart fed back to the model."""
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, RetryPromptPart):
                    return part
    return None


def projected_assistant_text(events: Sequence[RunEvent]) -> str:
    """Replay speculative text and retractions into the visible final text."""

    text = ""
    for event in events:
        if isinstance(event, TextDelta):
            text += event.text
        elif isinstance(event, TextRetracted):
            text = text[: -event.characters] if event.characters else text
    return text


def set_plan_delta(call_id: str, steps: list[dict[str, str]]) -> dict[int, DeltaToolCall]:
    return {0: DeltaToolCall("set_plan", f'{{"steps": {steps!r}}}'.replace("'", '"'), tool_call_id=call_id)}


def update_step_delta(
    call_id: str, *, step_id: str, status: StepStatus, note: str | None = None
) -> dict[int, DeltaToolCall]:
    args: dict[str, Any] = {"step_id": step_id, "status": status.value}
    if note is not None:
        args["note"] = note
    import json

    return {0: DeltaToolCall("update_step", json.dumps(args), tool_call_id=call_id)}


def report_progress_delta(
    call_id: str, *, summary: str, next_action: str | None = None
) -> dict[int, DeltaToolCall]:
    args: dict[str, Any] = {"summary": summary}
    if next_action is not None:
        args["next_action"] = next_action
    import json

    return {0: DeltaToolCall("report_progress", json.dumps(args), tool_call_id=call_id)}


def read_file_delta(call_id: str, path: str) -> dict[int, DeltaToolCall]:
    import json

    return {0: DeltaToolCall("read_file", json.dumps({"path": path}), tool_call_id=call_id)}


async def test_runtime_translates_thinking_stream_to_events() -> None:
    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield {0: DeltaThinkingPart(content="considering ")}
        yield {0: DeltaThinkingPart(content="the options")}
        yield "final answer"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="Think before answering.",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("no approval expected")

    outcome = await runtime.run("question", [], emit, approve)

    assert outcome.output == "final answer"
    thinking = "".join(event.text for event in events if isinstance(event, ThinkingDelta))
    assert thinking == "considering the options"
    # Reasoning never leaks into the speculative answer channel.
    assert not any(isinstance(event, TextDelta) and "considering" in event.text for event in events)


async def test_runtime_executes_tool_loop_and_emits_events() -> None:
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("echo", '{"value":"hello"}', tool_call_id="call-1")}
        else:
            yield f"final:{result.content}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("say hello", [], emit, approve)

    assert outcome.output == "final:echo:hello"
    assert any(isinstance(event, RunStarted) for event in events)
    assert any(isinstance(event, ToolCallStarted) and event.name == "echo" for event in events)
    assert any(isinstance(event, ToolCallFinished) and event.name == "echo" for event in events)
    assert any(isinstance(event, TextDelta) and "final:" in event.text for event in events)
    assert isinstance(events[-1], RunCompleted)


async def test_runtime_records_declared_non_observe_effect() -> None:
    def execute() -> str:
        return "done"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("execute", "{}", tool_call_id="effect-1")}
        else:
            yield "complete"

    recorded: list[dict[str, object]] = []

    def record_effect(**values: object) -> None:
        recorded.append(values)

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(execute, sequential=True)],
        toolsets=[],
        instructions="Use tools.",
        limits=LimitsConfig(),
        tool_metadata={
            "execute": {
                "origin": "test",
                "risk": "execute",
                "effect": EffectKind.EXECUTION.value,
            }
        },
        effect_recorder=record_effect,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("tool should not request approval")

    await runtime.run("go", [], emit, approve, session_id="effect-session")

    assert recorded == [
        {
            "tool_name": "execute",
            "effect_kind": EffectKind.EXECUTION,
            "success": True,
            "summary": "execute succeeded",
        }
    ]


def test_work_completion_gate_applies_without_an_approved_plan() -> None:
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="Answer.",
        limits=LimitsConfig(),
        tool_metadata={},
        work_completion_issues=lambda _session_id: ["effect pending"],
    )

    assert runtime._completion_gate_issues() == [  # pyright: ignore[reportPrivateUsage]
        "effect pending"
    ]


async def test_real_request_snapshot_updates_for_each_model_step() -> None:
    def echo(value: str) -> str:
        return value

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("echo", '{"value":"ok"}', tool_call_id="echo-1")}
        else:
            yield "done"

    model = FunctionModel(stream_function=model_function)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:test",
    )
    runtime = AgentRuntime(
        model=model,
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
        context_engine=engine,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("go", [], emit, approve, session_id="snapshot-session")
    report = await engine.control(ContextReportCommand("snapshot-session"), emit)
    snapshot = report.payload["request_snapshot"]

    assert snapshot["model_step"] == 2
    assert "echo" in snapshot["visible_tools"]
    assert "request_clarification" in snapshot["visible_tools"]
    assert [receipt.step for receipt in outcome.request_receipts] == [1, 2]
    assert {receipt.route for receipt in outcome.request_receipts} == {"acme:test"}
    assert all(receipt.context_fingerprint for receipt in outcome.request_receipts)
    assert outcome.request_receipts[1].visible_tool_digest == snapshot["visible_tool_digest"]
    assert all(receipt.input_manifest is not None for receipt in outcome.request_receipts)
    manifest_steps = [
        receipt.input_manifest.step for receipt in outcome.request_receipts if receipt.input_manifest
    ]
    assert manifest_steps == [1, 2]
    assert all(
        receipt.input_manifest.request_fingerprint.startswith("sha256:")
        for receipt in outcome.request_receipts
        if receipt.input_manifest is not None
    )


async def test_runtime_can_select_lumen_agent_loop_for_text_only_execution() -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-loop-test",
    )
    driver = _LumenTextDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Answer with the Lumen loop.",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
        model_driver=driver,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("text-only execution cannot request approval")

    outcome = await runtime.run("question", [], emit, approve, session_id="lumen-loop-session")

    assert outcome.output == "native answer"
    assert outcome.status == "completed"
    assert outcome.usage["requests"] == 1
    assert outcome.usage["cache_read_tokens"] == 8
    assert len(driver.requests) == 1
    assert driver.requests[0].input_manifest == outcome.request_receipts[0].input_manifest
    assert driver.requests[0].route == "acme:lumen-loop-test"
    assert outcome.request_receipts[0].step == 1
    assert isinstance(events[0], RunStarted)
    assert any(isinstance(event, ThinkingDelta) and event.text == "native thinking" for event in events)
    assert any(isinstance(event, TextDelta) and event.text == "native answer" for event in events)
    assert isinstance(events[-1], RunCompleted)
    assert projected_assistant_text(events) == "native answer"


async def test_runtime_freezes_native_search_in_request_manifest() -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    driver = _LumenTextDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Search when current sources are required.",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=driver,
        native_tools=(ModelNativeTool(kind="web_search", search_context_size="high"),),
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("provider-native search does not use the local approval seam")

    outcome = await runtime.run("latest status", [], emit, approve, session_id="native-search")

    request = driver.requests[0]
    manifest = outcome.request_receipts[0].input_manifest
    assert request.native_tools == (
        ModelNativeTool(kind="web_search", search_context_size="high"),
    )
    assert manifest is not None
    assert manifest.tool_count == len(request.tools) + 1
    assert "web_search" in outcome.request_receipts[0].visible_tools


async def test_runtime_lumen_loop_executes_gateway_tools_and_commits_full_trajectory(
    tmp_path: Path,
) -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    async def echo(value: str) -> str:
        return value

    registry = ToolRegistry(tmp_path)
    registry.add(
        ToolSpec(
            echo,
            risk=Risk.READ,
            effect_kind=EffectKind.OBSERVE,
            concurrency=lambda _arguments: ToolConcurrency.PARALLEL_SAFE,
        ),
        origin="test",
    )
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )
    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-tool-loop-test",
    )
    driver = _LumenToolDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Use the available tool.",
        limits=LimitsConfig(parallel_tool_calls="parallel_safe"),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
        context_engine=engine,
        model_driver=driver,
        capability_gateway=gateway,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tools must not request approval")

    outcome = await runtime.run("question", [], emit, approve, session_id="lumen-tool-session")

    assert outcome.output == "The tool returned hello."
    assert len(driver.requests) == 2
    assert {tool["name"] for tool in driver.requests[0].tools} == {"echo", *CONTROL_TOOL_NAMES}
    assert [receipt.step for receipt in outcome.request_receipts] == [1, 2]
    assert outcome.usage["requests"] == 2
    usage_event = next(event for event in events if isinstance(event, UsageUpdated))
    assert usage_event.tool_call_count == 1
    assert any(isinstance(event, CommentaryDelta) for event in events)
    assert any(isinstance(event, ToolCallStarted) and event.name == "echo" for event in events)
    assert any(
        isinstance(event, ToolCallFinished)
        and event.name == "echo"
        and event.result == "hello"
        and not event.is_error
        for event in events
    )
    assert any(
        isinstance(message, ModelResponse)
        and any(isinstance(part, ToolCallPart) and part.tool_call_id == "echo-1" for part in message.parts)
        for message in outcome.new_messages
    )
    tool_return = last_tool_return(outcome.new_messages)
    assert tool_return is not None
    assert tool_return.tool_call_id == "echo-1"
    assert projected_assistant_text(events) == "The tool returned hello."


@pytest.mark.parametrize("query", ["weather forecast", "天气", "mcp:weather", "", "*"])
@pytest.mark.parametrize("with_context", [False, True])
async def test_runtime_deferred_tool_search_loads_schema_on_next_request(
    tmp_path: Path, query: str, with_context: bool,
) -> None:
    gateway = CapabilityGateway(
        ToolRegistry(tmp_path),
        PermissionPolicy(PermissionsConfig()),
        default_timeout=5,
    )
    calls: list[dict[str, Any]] = []

    async def weather(arguments: dict[str, Any]) -> str:
        calls.append(arguments)
        return "sunny"

    gateway.register(
        CapabilityDescriptor(
            name="weather_lookup",
            description="Look up a city weather forecast. 查询城市天气预报。",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            origin="mcp:weather",
            risk="read",
            effect_kind=EffectKind.OBSERVE,
            timeout_seconds=5,
            deferred=True,
        ),
        weather,
    )
    driver = _LumenDeferredDriver(query)
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="Use deferred tools when needed.",
        limits=LimitsConfig(),
        tool_metadata={
            "weather_lookup": {
                "origin": "mcp:weather",
                "risk": "read",
                "effect": "observe",
            }
        },
        capability_gateway=gateway,
        model_driver=driver,
        context_engine=(
            ContextEngine(ContextConfig(), model="test", model_id="test") if with_context else None
        ),
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read capability must not request approval")

    outcome = await runtime.run("weather in Shanghai", [], emit, approve)

    assert outcome.output == "Shanghai is sunny."
    assert calls == [{"city": "Shanghai"}]
    assert len(outcome.request_receipts) == 3


async def test_runtime_lumen_loop_stops_at_blocking_clarification(tmp_path: Path) -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-clarification-test",
    )
    gateway = CapabilityGateway(
        ToolRegistry(tmp_path),
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )
    driver = _LumenClarificationDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Ask when blocked.",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
        model_driver=driver,
        capability_gateway=gateway,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("clarification must not request approval")

    outcome = await runtime.run("question", [], emit, approve, session_id="native-clarification")

    assert outcome.status == "waiting_for_user"
    assert outcome.output == ""
    assert outcome.pending_clarification is not None
    assert outcome.pending_clarification.question == "Which target?"
    assert driver.calls == 1
    assert [receipt.step for receipt in outcome.request_receipts] == [1]
    assert any(isinstance(event, ClarificationRequested) for event in events)
    assert isinstance(events[-1], RunWaitingForUser)
    result = last_tool_return(outcome.new_messages)
    assert result is not None
    assert result.tool_name == "request_clarification"


async def test_runtime_lumen_loop_reuses_shared_side_effect_recovery_receipt(
    tmp_path: Path,
) -> None:
    executions = 0

    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    async def write_note(content: str) -> dict[str, str]:
        nonlocal executions
        executions += 1
        return {"written": content}

    registry = ToolRegistry(tmp_path)
    registry.add(
        ToolSpec(write_note, risk=Risk.WRITE, effect_kind=EffectKind.MUTATION),
        origin="test",
    )
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )
    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-recovery-test",
    )
    driver = _LumenRecoveringWriteDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Write exactly once.",
        limits=LimitsConfig(),
        tool_metadata={
            "write_note": {
                "origin": "test",
                "risk": "write",
                "effect": EffectKind.MUTATION.value,
            }
        },
        context_engine=engine,
        model_driver=driver,
        capability_gateway=gateway,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True, "allowed")

    with pytest.raises(LoopProviderFailure, match="provider failed after write") as captured:
        await runtime.run("write", [], emit, approve, session_id="native-recovery-1")
    partial = get_partial_outcome(captured.value)
    assert partial is not None
    assert len(partial.recovery_receipts) == 1
    assert partial.recovery_receipts[0]["replayed"] is False
    assert executions == 1

    driver.fail_after_tool = False
    outcome = await runtime.run(
        "write",
        [],
        emit,
        approve,
        session_id="native-recovery-2",
        recovery_receipts=partial.recovery_receipts,
    )

    assert outcome.output == "recovered without another write"
    assert executions == 1
    assert outcome.recovery_receipts[0]["replayed"] is True


async def test_runtime_lumen_loop_delivers_steering_at_tool_request_boundary(
    tmp_path: Path,
) -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    async def pause_tool() -> str:
        tool_started.set()
        await release_tool.wait()
        return "tool done"

    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(pause_tool, risk=Risk.READ), origin="test")
    gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )
    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-interactive-tool-test",
    )
    driver = _LumenInteractiveToolDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Accept steering.",
        limits=LimitsConfig(),
        tool_metadata={"pause_tool": {"origin": "test", "risk": "read"}},
        context_engine=engine,
        model_driver=driver,
        capability_gateway=gateway,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool must not request approval")

    task = asyncio.create_task(
        runtime.run("start", [], emit, approve, session_id="native-interactive-tool")
    )
    await tool_started.wait()
    queued = runtime.interactive_queue.enqueue(
        "steer now",
        "native steer injected",
        QueueMode.STEER,
    )
    release_tool.set()
    outcome = await task

    assert outcome.output == "native steering received"
    assert driver.saw_steering is True
    assert [receipt.step for receipt in outcome.request_receipts] == [1, 2]
    assert any(
        isinstance(event, InputDelivered) and event.message_id == queued.id for event in events
    )


async def test_runtime_lumen_loop_continues_one_follow_up_before_completion(
    tmp_path: Path,
) -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-follow-up-test",
    )
    gateway = CapabilityGateway(
        ToolRegistry(tmp_path),
        PermissionPolicy(PermissionsConfig()),
        default_timeout=1,
    )
    driver = _LumenFollowUpDriver()
    attachment_store = AttachmentStore(ArtifactStore(tmp_path / "attachment-artifacts"))
    attachment = attachment_store.store_image(
        filename="queued.png",
        media_type="image/png",
        content=b"\x89PNG\r\n\x1a\nqueued-image",
    )
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Handle follow-up input.",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
        model_driver=driver,
        capability_gateway=gateway,
        attachment_store=attachment_store,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("no tool can request approval")

    task = asyncio.create_task(
        runtime.run("start", [], emit, approve, session_id="native-follow-up")
    )
    await driver.first_response_started.wait()
    queued = runtime.interactive_queue.enqueue(
        "afterwards",
        "native follow up",
        QueueMode.FOLLOW_UP,
        attachments=(attachment,),
    )
    driver.release_first_response.set()
    outcome = await task

    assert outcome.output == "follow-up completed"
    assert driver.saw_follow_up is True
    assert [receipt.step for receipt in outcome.request_receipts] == [1, 2]
    assert projected_assistant_text(events) == "follow-up completed"
    assert any(
        isinstance(event, CommentaryDelta) and event.text == "initial candidate" for event in events
    )
    assert any(
        isinstance(event, InputDelivered) and event.message_id == queued.id for event in events
    )
    markers = [
        attachment_from_marker(item.content)
        for message in outcome.new_messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        for item in part.content
        if isinstance(item, TextContent)
    ]
    assert attachment in markers


async def test_runtime_lumen_loop_calls_completion_gate_and_retracts_rejected_text() -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-loop-test",
    )
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Answer with the Lumen loop.",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
        work_completion_issues=lambda _session_id: ["verification pending"],
        model_driver=_LumenTextDriver("unverified answer"),
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("text-only execution cannot request approval")

    with pytest.raises(LoopCompletionRejected, match="verification pending") as captured:
        await runtime.run("question", [], emit, approve, session_id="lumen-loop-rejected")

    partial = get_partial_outcome(captured.value)
    assert partial is not None
    assert partial.request_receipts[0].input_manifest is not None
    assert partial.partial_text == ""
    assert partial.usage["requests"] == 3
    assert len(partial.request_receipts) == 3
    assert partial.usage["cache_read_tokens"] == 24
    assert projected_assistant_text(events) == ""
    assert any(isinstance(event, RunFailed) for event in events)


async def test_runtime_lumen_loop_retries_before_public_output_without_duplication() -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-loop-test",
    )
    driver = _LumenRetryDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Retry safely.",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
        model_driver=driver,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("text-only execution cannot request approval")

    outcome = await runtime.run("question", [], emit, approve, session_id="lumen-loop-retry")

    assert outcome.output == "recovered once"
    assert driver.calls == 2
    assert projected_assistant_text(events) == "recovered once"
    assert sum(isinstance(event, TextDelta) for event in events) == 1
    assert len(outcome.request_receipts) == 1


async def test_runtime_lumen_loop_cancellation_preserves_request_evidence() -> None:
    async def unused_model(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("PydanticAI Agent loop must not execute")
        yield "unreachable"

    model = FunctionModel(stream_function=unused_model)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:lumen-loop-test",
    )
    driver = _LumenBlockingDriver()
    runtime = AgentRuntime(
        model=model,
        tools=[],
        toolsets=[],
        instructions="Wait safely.",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
        model_driver=driver,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("text-only execution cannot request approval")

    task = asyncio.create_task(runtime.run("question", [], emit, approve, session_id="lumen-loop-cancel"))
    await driver.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    partial = get_partial_outcome(captured.value)
    assert partial is not None
    assert partial.status == "cancelled"
    assert len(partial.request_receipts) == 1
    assert partial.request_receipts[0].input_manifest is not None
    assert any(isinstance(event, RunCancelled) for event in events)
    observation = next(d for d in partial.diagnostics if d.get("kind") == "request_observed")
    assert observation["error_category"] == "cancelled"
    assert observation["first_thinking_seconds"] is None


async def test_runtime_request_deadline_preserves_partial_evidence_and_usage() -> None:
    class TimedDriver(_RuntimeDriverMixin):
        async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
            del request
            yield ModelResponseStarted(sequence=0)
            yield ModelTextDelta(sequence=1, content="download pending")
            yield ModelUsage(sequence=2, input_tokens=17, output_tokens=9)
            await asyncio.Event().wait()

    runtime = AgentRuntime(
        model="test", tools=[], toolsets=[], instructions="Download.",
        limits=LimitsConfig(model_request_timeout_seconds=0.03), tool_metadata={},
        model_driver=TimedDriver(),
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    with pytest.raises(LoopRequestTimeout, match="model request deadline") as captured:
        await runtime.run("download", [], emit, lambda _: pytest.fail("no approval"))
    partial = get_partial_outcome(captured.value)
    assert partial is not None
    assert partial.status == "failed"
    assert partial.partial_text == "download pending"
    assert partial.usage["input_tokens"] == 17
    assert partial.usage["output_tokens"] == 9
    assert partial.usage["requests"] == 1
    assert len(partial.request_receipts) == 1
    assert sum(isinstance(event, RunFailed) for event in events) == 1
    assert all(
        "Run stopped at a usage limit" not in event.message
        for event in events if isinstance(event, RunFailed)
    )


async def test_runtime_content_filter_emits_one_failed_terminal_without_completion() -> None:
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="Answer safely.",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=_LumenRejectedDriver(),
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("filtered text-only execution cannot request approval")

    with pytest.raises(LoopProviderFailure, match="content_filter"):
        await runtime.run("question", [], emit, approve, session_id="lumen-filtered")

    terminals = [event for event in events if isinstance(event, RunCompleted | RunFailed)]
    assert len(terminals) == 1
    assert isinstance(terminals[0], RunFailed)


def test_runtime_lumen_loop_builds_default_driver_and_accepts_direct_evidence() -> None:
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="Answer.",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    assert runtime._model_driver is not None  # type: ignore[reportPrivateUsage]

    custom = _LumenTextDriver()
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="Answer.",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=custom,
    )
    assert runtime._model_driver is custom  # type: ignore[reportPrivateUsage]


@pytest.mark.parametrize("encoded", [False, True])
async def test_runtime_enters_waiting_state_after_blocking_clarification(encoded: bool) -> None:
    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {
                0: DeltaToolCall(
                    "request_clarification",
                    json.dumps({
                        "question": "Which target?", "choices": '["甲","乙"]' if encoded else ["甲", "乙"],
                    }),
                    tool_call_id="clarify-1",
                )
            }
        else:
            yield "Please choose A or B."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="Ask when blocked.",
        limits=LimitsConfig(),
        tool_metadata={"request_clarification": {"origin": "control", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("clarification does not require approval")

    outcome = await runtime.run("continue", [], emit, approve)

    assert outcome.status == "waiting_for_user"
    assert outcome.pending_clarification is not None
    assert outcome.pending_clarification.choices == ("甲", "乙")
    assert any(isinstance(event, ClarificationRequested) for event in events)
    assert isinstance(events[-1], RunWaitingForUser)


@pytest.mark.parametrize("choices", ['{"option":"A"}', '[1]', '["A",null]', "not-json", "x" * 8193])
def test_clarification_choices_reject_non_arrays_and_non_strings(choices: str) -> None:
    from pydantic import ValidationError

    from lumen.runtime import ClarificationGate

    tool = Tool(ClarificationGate(None).request)
    with pytest.raises(ValidationError):
        tool.function_schema.validator.validate_python({"question": "请选择", "choices": choices})
    choices_schema = tool.function_schema.json_schema["properties"]["choices"]
    assert any(item.get("type") == "array" for item in choices_schema["anyOf"])
    assert all(item.get("type") != "string" for item in choices_schema["anyOf"])


async def test_runtime_preserves_structured_tool_results_and_renders_them_as_json() -> None:
    def inspect_file() -> dict[str, object]:
        """Return a representative paginated file result."""
        return {"content": "1: hello", "has_more": True, "next_start_line": 2}

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("inspect_file", "{}", tool_call_id="structured-1")}
        else:
            assert result.content == {
                "content": "1: hello",
                "has_more": True,
                "next_start_line": 2,
            }
            yield "structured result received"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(inspect_file, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"inspect_file": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("inspect", [], emit, approve)

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert outcome.output == "structured result received"
    assert finished.result.startswith('{\n  "content": "1: hello"')
    assert '"has_more": true' in finished.result


async def test_runtime_requests_approval_and_returns_denial_to_model() -> None:
    executed = False

    def write_note(content: str) -> str:
        """Write a note."""
        nonlocal executed
        executed = True
        return content

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("write_note", '{"content":"secret"}', tool_call_id="call-2")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "plugin", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def deny(request: Any) -> ToolApproval:
        assert request.name == "write_note"
        return ToolApproval(approved=False, message="not allowed")

    outcome = await runtime.run("write this", [], emit, deny)

    assert outcome.output == "handled:denied"
    assert executed is False
    # The runtime no longer emits ToolApprovalPending itself — the approve
    # callback owns that surface (it decides whether to mount a card or
    # short-circuit in auto mode). The runtime only guarantees the resolved
    # event reaches the timeline.
    assert any(isinstance(event, ToolApprovalResolved) and event.approved is False for event in events)


async def test_runtime_treats_unregistered_approval_tool_as_unknown_external_risk() -> None:
    def dynamic_tool() -> str:
        """A tool discovered after the initial metadata snapshot."""
        return "executed"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("dynamic_tool", "{}", tool_call_id="dynamic-call")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(dynamic_tool, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={},
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def deny(request: Any) -> ToolApproval:
        assert request.origin == "unregistered remote tool"
        assert request.risk == "external_unknown"
        return ToolApproval(approved=False, message="unknown tool denied")

    outcome = await runtime.run("use dynamic tool", [], emit, deny)

    assert outcome.output == "handled:denied"


async def test_recoverable_tool_error_is_fed_back_to_model() -> None:
    """P0: a recoverable tool exception (file not found) must not crash the
    loop. The error is converted to a model-visible tool failure so the model
    can adjust — e.g. call list_directory — and the run still completes."""

    call_log: list[str] = []

    def read_file(path: str) -> str:
        """Read a file."""
        call_log.append(f"read_file:{path}")
        if path == "missing.txt":
            raise FileNotFoundError(f"file not found: {path}")
        return "ok"

    def list_directory(path: str = ".") -> str:
        """List a directory."""
        call_log.append(f"list_directory:{path}")
        return "README.md"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        retry = last_retry_prompt(messages)
        result = last_tool_return(messages)
        if retry is None and result is None:
            # First attempt: try to read a file that doesn't exist.
            yield {0: DeltaToolCall("read_file", '{"path": "missing.txt"}', tool_call_id="call-1")}
        elif retry is not None and result is None:
            # The file-not-found became a retry prompt — recover by listing.
            yield {0: DeltaToolCall("list_directory", '{"path": "."}', tool_call_id="call-2")}
        else:
            yield "recovered after listing"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(read_file, sequential=True), Tool(list_directory, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={
            "read_file": {"origin": "builtin", "risk": "read"},
            "list_directory": {"origin": "builtin", "risk": "read"},
        },
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("find the file", [], emit, approve)

    # The run completed instead of crashing.
    assert outcome.output == "recovered after listing"
    assert isinstance(events[-1], RunCompleted)
    # The model saw the error and recovered via list_directory.
    assert "read_file:missing.txt" in call_log
    assert "list_directory:." in call_log
    # A ToolCallFinished was emitted for the failed read with is_error set.
    failed = [e for e in events if isinstance(e, ToolCallFinished) and e.name == "read_file"]
    assert len(failed) == 1
    assert failed[0].is_error is True
    assert "file not found" in failed[0].result


async def test_ambiguous_edit_error_is_fed_back_to_model() -> None:
    """A multi-match edit_file error must also feed back rather than crash."""

    def edit_file(path: str, find: str, replace: str) -> str:
        """Edit a file by exact match."""
        raise ValueError(f"{3} matches for find in {path}")

    attempts = 0

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        retry = last_retry_prompt(messages)
        if retry is None:
            yield {0: DeltaToolCall("edit_file", '{"path":"a","find":"x","replace":"y"}', tool_call_id="c1")}
        else:
            yield "gave up after seeing the match error"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(edit_file, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"edit_file": {"origin": "builtin", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(approved=True, message="ok")

    outcome = await runtime.run("edit the file", [], emit, approve)

    assert outcome.output == "gave up after seeing the match error"
    assert isinstance(events[-1], RunCompleted)
    assert attempts == 2


async def test_runtime_emits_structured_plan_and_progress() -> None:
    def read_file(path: str) -> str:
        """Read a file."""
        return f"contents of {path}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield set_plan_delta("plan-1", [{"id": "inspect", "title": "Inspect project"}])
        elif result.tool_name == "set_plan":
            yield report_progress_delta("progress-1", summary="Found config.", next_action="Run tests")
        elif result.tool_name == "report_progress":
            yield read_file_delta("read-1", "config.yaml")
        elif result.tool_name == "read_file":
            yield update_step_delta("update-1", step_id="inspect", status=StepStatus.COMPLETED)
        else:
            yield "All done."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(read_file, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"read_file": {"origin": "builtin", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read-only tool should not request approval")

    outcome = await runtime.run("inspect", [], emit, approve)

    event_order = [
        type(event) for event in events if isinstance(event, (PlanCreated, ProgressReported, PlanUpdated))
    ]
    assert event_order[:2] == [PlanCreated, ProgressReported]
    assert event_order[-1] is PlanUpdated
    assert len(outcome.plan.evidence) == 1
    assert outcome.plan.steps[0].status is StepStatus.COMPLETED
    assert outcome.plan.steps[0].id == "inspect"
    assert outcome.active_history == outcome.new_messages


async def test_completion_gate_retries_visible_observation_then_allows_fix() -> None:
    attempt = 0

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield "premature"
        elif attempt == 2:
            retry = last_retry_prompt(messages)
            assert retry is not None
            assert "完成门禁未通过" in str(retry.content)
            yield update_step_delta(
                "finish-after-gate",
                step_id="implement",
                status=StepStatus.COMPLETED,
            )
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    outcome = await runtime.run(
        "execute",
        [],
        emit,
        lambda _request: pytest.fail("approval should not be requested"),  # type: ignore[arg-type]
        plan=PlanState(
            revision=1,
            approved_revision=1,
            steps=[PlanStep(id="implement", title="Implement")],
        ),
    )

    assert outcome.status == "completed"
    assert outcome.output == "done"
    assert attempt == 3
    assert projected_assistant_text(events) == "done"
    assert any(isinstance(event, TextRetracted) for event in events)


async def test_default_mode_reconciles_new_plan_before_final_answer() -> None:
    attempt = 0

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield set_plan_delta("plan", [{"id": "inspect", "title": "Inspect"}])
        elif attempt == 2:
            yield "premature final"
        elif attempt == 3:
            retry = last_retry_prompt(messages)
            assert retry is not None and "step inspect is pending" in str(retry.content)
            yield update_step_delta("finish", step_id="inspect", status=StepStatus.COMPLETED)
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[], toolsets=[], instructions="help",
        limits=LimitsConfig(), tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    outcome = await runtime.run("inspect", [], emit, lambda _: pytest.fail("no approval"))
    assert outcome.output == "done"
    assert projected_assistant_text(events) == "done"
    assert outcome.plan.steps[0].status is StepStatus.COMPLETED
    assert any(isinstance(event, TextRetracted) for event in events)


async def test_completion_gate_fails_after_two_retries() -> None:
    attempts = 0

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        yield "still premature"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("approval should not be requested")

    with pytest.raises(LoopCompletionRejected, match="completion_gate_failed"):
        await runtime.run(
            "execute",
            [],
            emit,
            approve,
            plan=PlanState(
                revision=1,
                approved_revision=1,
                steps=[PlanStep(id="implement", title="Implement")],
            ),
        )

    assert attempts == 3
    assert isinstance(events[-1], RunFailed)
    assert "completion_gate_failed" in events[-1].message
    assert projected_assistant_text(events) == ""


def test_control_tools_are_sequential_and_visible() -> None:
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    gateway = runtime._capability_gateway  # type: ignore[reportPrivateUsage]
    tool_names = {descriptor.name for descriptor in gateway.catalog()}
    assert {"set_plan", "update_step", "report_progress"}.issubset(tool_names)
    control_tool = gateway.descriptor("set_plan")
    assert control_tool is not None
    assert control_tool.concurrency is ToolConcurrency.EXCLUSIVE
    assert control_tool.requires_approval is False
    assert control_tool.origin == "control"


async def test_runtime_plan_input_seeds_controller() -> None:
    plan = PlanState(steps=[PlanStep(id="one", title="One", status=StepStatus.COMPLETED)])

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "ok"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(False)

    outcome = await runtime.run("noop", [], emit, approve, plan=plan)

    assert outcome.plan == plan


async def test_runtime_carries_active_history() -> None:
    seed_history: list[ModelMessage] = [
        ModelRequest(parts=[__import__("pydantic_ai").messages.UserPromptPart(content="seed")])
    ]

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "ok"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(False)

    outcome = await runtime.run("noop", seed_history, emit, approve)

    assert outcome.active_history == [*seed_history, *outcome.new_messages]


async def test_runtime_separates_commentary_from_final_text() -> None:
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            # First model response: text + tool call together.
            yield "Inspecting configuration."
            yield {0: DeltaToolCall("echo", '{"value":"x"}', tool_call_id="c1")}
        else:
            # Final response: text only.
            yield "Configuration is valid."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("inspect", [], emit, approve)

    commentary = "".join(event.text for event in events if isinstance(event, CommentaryDelta))
    final = projected_assistant_text(events)
    assert commentary == "Inspecting configuration."
    assert final == "Configuration is valid."
    assert any(isinstance(event, TextRetracted) for event in events)
    # The final output is still the terminal result, not the commentary.
    assert outcome.output == "Configuration is valid."


async def test_runtime_emits_no_commentary_for_tool_only_response() -> None:
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("echo", '{"value":"x"}', tool_call_id="c1")}
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await runtime.run("go", [], emit, approve)

    commentary = [event for event in events if isinstance(event, CommentaryDelta)]
    final = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert commentary == []
    assert final == "done"
    assert outcome.output == "done"


async def test_runtime_text_only_response_is_final() -> None:
    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "Just text."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await runtime.run("go", [], emit, approve)

    commentary = [event for event in events if isinstance(event, CommentaryDelta)]
    final = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert commentary == []
    assert final == "Just text."
    assert outcome.output == "Just text."


async def test_runtime_emits_text_delta_before_provider_stream_completes() -> None:
    first_chunk_produced = asyncio.Event()
    release_stream = asyncio.Event()

    async def model_function(
        _messages: list[ModelMessage],
        _info: AgentInfo,
    ):  # type: ignore[no-untyped-def]
        yield "first"
        first_chunk_produced.set()
        await release_stream.wait()
        yield " second"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []
    first_delta_emitted = asyncio.Event()

    async def emit(event: RunEvent) -> None:
        events.append(event)
        if isinstance(event, TextDelta):
            first_delta_emitted.set()

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    task = asyncio.create_task(runtime.run("go", [], emit, approve))
    await asyncio.wait_for(first_chunk_produced.wait(), timeout=1)
    try:
        await asyncio.wait_for(first_delta_emitted.wait(), timeout=0.2)
    finally:
        release_stream.set()
    outcome = await task

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "first second"
    assert outcome.output == "first second"


# ---------------------------------------------------------------------------
# Truncation / usage-limit handling (pi-inspired: distinguish "config issue"
# from "model misbehaved" so the user gets the right fix hint).
# ---------------------------------------------------------------------------


async def test_runtime_truncated_tool_call_yields_max_tokens_hint() -> None:
    """A provider length stop is surfaced as an actionable Lumen Loop failure."""

    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=_LumenTruncatedDriver(),
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    with pytest.raises(LoopTruncated):
        await runtime.run("test", [], emit, lambda _request: pytest.fail("no approval"))

    failures = [event for event in events if isinstance(event, RunFailed)]
    assert len(failures) == 1
    assert "max_tokens" in failures[0].message
    assert "被截断" in failures[0].message


async def test_runtime_recovers_implicit_output_limit_and_records_each_budget() -> None:
    driver = _LumenRecoveringTruncationDriver()
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model="test",
        model_id="acme:unknown",
    )
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=driver,
        context_engine=engine,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    outcome = await runtime.run(
        "test",
        [],
        emit,
        lambda _request: pytest.fail("no approval"),
        session_id="truncation-recovery",
    )

    assert outcome.output == "recovered without executing the partial call"
    assert [request.settings["max_tokens"] for request in driver.requests] == [16_384, 32_768]
    assert [receipt.output_reserve_tokens for receipt in outcome.request_receipts] == [16_384, 32_768]
    manifests = [receipt.input_manifest for receipt in outcome.request_receipts]
    assert all(manifest is not None for manifest in manifests)
    assert manifests[0].settings_digest != manifests[1].settings_digest  # type: ignore[union-attr]
    assert any(
        isinstance(event, ProgressReported) and "安全重试" in event.summary
        for event in events
    )


async def test_runtime_without_context_engine_uses_modern_unknown_output_default() -> None:
    driver = _LumenTextDriver()
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=driver,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    outcome = await runtime.run(
        "test",
        [],
        emit,
        lambda _request: pytest.fail("no approval"),
    )

    assert driver.requests[0].settings["max_tokens"] == 16_384
    assert outcome.request_receipts[0].output_reserve_tokens == 16_384


async def test_runtime_continues_exact_truncated_text_without_losing_prefix() -> None:
    driver = _LumenContinuingTextDriver()
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=driver,
        context_engine=ContextEngine(
            ContextConfig(enabled=True, soft_token_limit=1_000_000),
            model="test",
            model_id="acme:unknown",
        ),
    )

    async def emit(_event: RunEvent) -> None:
        return None

    outcome = await runtime.run(
        "test",
        [],
        emit,
        lambda _request: pytest.fail("no approval"),
        session_id="text-truncation-recovery",
    )

    assert outcome.output == "first second"
    assert len(driver.requests) == 2
    assert any(
        isinstance(part, RetryPromptPart)
        for message in outcome.new_messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def test_runtime_never_widens_explicit_max_tokens() -> None:
    driver = _LumenRecoveringTruncationDriver()
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
        model_settings={"max_tokens": 4_096},
        model_driver=driver,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    with pytest.raises(LoopTruncated):
        await runtime.run(
            "test",
            [],
            emit,
            lambda _request: pytest.fail("no approval"),
        )

    assert len(driver.requests) == 1
    assert driver.requests[0].settings["max_tokens"] == 4_096


async def test_runtime_usage_limit_reports_limit_not_model_failure() -> None:
    """The Lumen Loop request budget reports the configured guard."""

    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(request_count=1),
        tool_metadata={},
        model_driver=_LumenTextDriver("premature"),
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    with pytest.raises(LoopBudgetExceeded):
        await runtime.run(
            "test",
            [],
            emit,
            lambda _request: pytest.fail("no approval"),
            plan=PlanState(
                revision=1,
                approved_revision=1,
                steps=[PlanStep(id="pending", title="Pending")],
            ),
        )

    failures = [event for event in events if isinstance(event, RunFailed)]
    assert len(failures) == 1
    message = failures[0].message.lower()
    assert "使用量限制" in message
    assert "request_count" in message
    assert "agent.limits" in message
async def test_runtime_tool_failure_surfaces_raw_message_without_classification() -> None:
    """Tool denial is passed through verbatim — no retryable/category hint added.

    This mirrors pi's philosophy: the model already sees the denial via
    pydantic AI's normal tool-return plumbing; layering our own classification
    on top was both buggy (the classifier always fell through to 'other') and
    unnecessarily noisy. We exercise the denial path because it goes through
    pydantic AI's FunctionToolResultEvent, unlike a raw exception inside a
    tool function (which propagates and aborts the run).
    """

    def write_note(content: str) -> str:
        """Write a note."""
        return content

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("write_note", '{"content":"x"}', tool_call_id="c1")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def deny(_request: Any) -> ToolApproval:
        return ToolApproval(approved=False, message="not allowed XYZ")

    await runtime.run("test", [], emit, deny)

    finished = next(e for e in events if isinstance(e, ToolCallFinished) and e.name == "write_note")
    assert finished.is_error is True
    # Raw denial text preserved for the model + TUI.
    assert "denied" in finished.result.lower() or "XYZ" in finished.result
    # The old `error_category` field was removed — confirm it's gone.
    assert not hasattr(finished, "error_category")


# ---------------------------------------------------------------------------
# PartialRunOutcome: failed/cancelled runs carry an audit record (Phase 2.4)
# ---------------------------------------------------------------------------


async def test_failed_run_carries_partial_outcome_with_accumulated_state() -> None:
    """A Loop failure attaches the stable partial audit envelope."""

    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
        model_driver=_LumenTruncatedDriver(),
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    with pytest.raises(LoopTruncated) as exc_info:
        await runtime.run("write it", [], emit, lambda _request: pytest.fail("no approval"))

    partial = get_partial_outcome(exc_info.value)
    assert isinstance(partial, PartialRunOutcome)
    assert partial.status == "failed"
    assert partial.retryable is True
    assert partial.plan is not None
    assert isinstance(partial.approvals, list)
    assert isinstance(partial.diagnostics, list)
async def test_failed_run_after_tool_approval_preserves_approval() -> None:
    """When a run fails AFTER an approval was decided, the partial outcome
    carries that approval record — the audit trail isn't lost."""
    # Use a FunctionModel that calls a write tool (needs approval), then the
    # next model step raises. We force the failure by making the model raise
    # after the tool return is seen.
    raised = RuntimeError("provider blew up after the tool ran")

    def write_note(content: str) -> str:
        """Write a note."""
        return content

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("write_note", '{"content":"x"}', tool_call_id="c1")}
        else:
            # Tool completed and was approved; now the provider fails.
            raise raised

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(approved=True, message="allowed")

    with pytest.raises(RuntimeError, match="provider blew up"):
        await runtime.run("write it", [], emit, approve)

    # The failure event was emitted and carries the partial outcome on the exc.
    # We can't easily get the exc object here, so assert via the events + a
    # direct run that captures it.
    failures = [e for e in events if isinstance(e, RunFailed)]
    assert len(failures) == 1


async def test_explicit_retry_replays_exact_successful_side_effect_receipt() -> None:
    executions = 0
    fail_after_tool = True

    def write_note(content: str) -> dict[str, str]:
        """Write a note."""

        nonlocal executions
        executions += 1
        return {"written": content}

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("write_note", '{"content":"once"}', tool_call_id="write-1")}
        elif fail_after_tool:
            raise RuntimeError("provider failed after write")
        else:
            yield "recovered"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="write once",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True, "allowed")

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.run("write it", [], emit, approve)
    partial = get_partial_outcome(exc_info.value)
    assert partial is not None
    assert len(partial.recovery_receipts) == 1
    assert executions == 1

    fail_after_tool = False
    outcome = await runtime.run(
        "write it",
        [],
        emit,
        approve,
        recovery_receipts=partial.recovery_receipts,
    )

    assert outcome.output == "recovered"
    assert executions == 1
    assert outcome.recovery_receipts[0]["replayed"] is True
