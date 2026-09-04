from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ContentFilterError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from lumen.agent_loop import (
    ModelDriverRequest,
    ModelProviderError,
    ModelResponseCompleted,
    ModelResponseStarted,
    ModelStopReason,
    ModelTextDelta,
    ModelToolCallCompleted,
    ModelToolCallStarted,
    ModelUsage,
    PydanticAIModelDriver,
)
from lumen.context import ModelInputManifest, ReplayEligibility


def _manifest(*, tool_count: int = 0) -> ModelInputManifest:
    digest = "sha256:" + "0" * 64
    return ModelInputManifest(
        session_id="driver-session",
        step=1,
        route="test:model",
        provider="test",
        model="model",
        context_fingerprint="context-one",
        message_count=1,
        tool_count=tool_count,
        instructions_digest=digest,
        message_history_digest=digest,
        tool_schema_digest=digest,
        context_sources_digest=digest,
        stable_prefix_digest=digest,
        dynamic_tail_digest=digest,
        request_fingerprint="sha256:" + "1" * 64,
        replay_eligibility=ReplayEligibility.VERIFY_ONLY,
        non_replayable_reasons=("provider_private_framing_not_captured",),
    )


def _request(*, tools: tuple[dict[str, Any], ...] = ()) -> ModelDriverRequest[ModelMessage]:
    return ModelDriverRequest(
        request_id="request-one",
        route="test:model",
        messages=(ModelRequest(parts=[UserPromptPart(content="hello")]),),
        instructions="Follow the system instructions.",
        tools=tools,
        input_manifest=_manifest(tool_count=len(tools)),
        settings={"temperature": 0.1},
    )


async def _collect(driver: PydanticAIModelDriver, request: ModelDriverRequest[ModelMessage]) -> list[Any]:
    async with driver:
        async with driver.open_stream(request) as stream:
            return [event async for event in stream.events]


async def test_sdk_retries_are_disabled_and_raw_http_idle_and_retry_after_are_preserved() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503, json={"error": {"message": "busy"}}, headers={"retry-after": "12"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sdk = AsyncOpenAI(api_key="test", http_client=client, max_retries=4)
        driver = PydanticAIModelDriver(OpenAIChatModel("test", provider=OpenAIProvider(openai_client=sdk)))
        events = await _collect(driver, _request())
    assert len(seen) == 1
    assert sdk.max_retries == 0
    assert seen[0].extensions["timeout"]["read"] == 300
    assert seen[0].extensions["timeout"]["connect"] == 10
    assert isinstance(events[-1], ModelProviderError)
    assert events[-1].retry_after_seconds == 12


async def test_sse_heartbeats_do_not_trigger_model_event_idle_timeout() -> None:
    import asyncio
    import json

    from lumen.agent_loop import LoopLimits, LumenAgentLoop

    class Heartbeats(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(8):
                await asyncio.sleep(0.01)
                yield b": ping\n\n"
            chunk = {
                "id": "one", "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
            }
            yield ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Heartbeats()),
    )) as client:
        driver = PydanticAIModelDriver(OpenAIChatModel(
            "test", provider=OpenAIProvider(openai_client=AsyncOpenAI(api_key="test", http_client=client)),
        ))
        async with driver:
            result = await LumenAgentLoop(driver, limits=LoopLimits(
                model_stream_idle_timeout_seconds=0.03,
            )).run(_request())
    assert result.output == "done"
    assert result.model_attempts == 1


async def test_pydantic_driver_streams_text_usage_and_instructions() -> None:
    model = TestModel(custom_output_text="native text")

    events = await _collect(PydanticAIModelDriver(model), _request())

    assert isinstance(events[0], ModelResponseStarted)
    assert "".join(event.content for event in events if isinstance(event, ModelTextDelta)) == "native text"
    assert isinstance(events[-2], ModelUsage)
    assert isinstance(events[-1], ModelResponseCompleted)
    assert events[-1].stop_reason is ModelStopReason.END_TURN
    assert [event.sequence for event in events] == list(range(len(events)))
    assert model.last_model_request_parameters is not None
    assert model.last_model_request_parameters.instruction_parts is not None
    assert model.last_model_request_parameters.instruction_parts[0].content == (
        "Follow the system instructions."
    )


async def test_pydantic_driver_returns_exact_response_and_owns_model_lifecycle() -> None:
    class LifecycleModel(TestModel):
        entered = 0
        exited = 0
        prepared = 0

        async def __aenter__(self) -> LifecycleModel:
            self.entered += 1
            return await super().__aenter__()

        async def __aexit__(self, *args: Any) -> bool | None:
            self.exited += 1
            return await super().__aexit__(*args)

        def prepare_messages(self, messages: list[ModelMessage]) -> list[ModelMessage]:
            self.prepared += 1
            return super().prepare_messages(messages)

    model = LifecycleModel(custom_output_text="exact response")
    driver = PydanticAIModelDriver(model)
    async with driver:
        async with driver.open_stream(_request()) as stream:
            _ = [event async for event in stream.events]
            response = stream.response

    assert isinstance(response, ModelResponse)
    assert driver.response_text(response) == "exact response"
    assert model.entered == 1
    assert model.exited == 1
    assert model.prepared == 1


async def test_pydantic_driver_translates_tool_calls_without_executing_them() -> None:
    model = TestModel(call_tools=["echo"])
    tool = {
        "name": "echo",
        "description": "Echo a value.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }

    events = await _collect(PydanticAIModelDriver(model), _request(tools=(tool,)))

    started = next(event for event in events if isinstance(event, ModelToolCallStarted))
    completed = next(event for event in events if isinstance(event, ModelToolCallCompleted))
    assert started.name == "echo"
    assert completed.call_id == started.call_id
    assert completed.arguments == {"value": "a"}
    assert isinstance(events[-1], ModelResponseCompleted)
    assert events[-1].stop_reason is ModelStopReason.TOOL_CALL


class _FailingTestModel(TestModel):
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: object | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        del messages, model_settings, model_request_parameters, run_context
        raise ModelHTTPError(503, "test", {"error": "unavailable"})
        yield  # pragma: no cover


async def test_pydantic_driver_classifies_retryable_provider_errors() -> None:
    events = await _collect(PydanticAIModelDriver(_FailingTestModel()), _request())

    assert isinstance(events[0], ModelResponseStarted)
    assert isinstance(events[-1], ModelProviderError)
    assert events[-1].category == "http_503"
    assert events[-1].retryable is True


class _FilteredTestModel(TestModel):
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: object | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        del messages, model_settings, model_request_parameters, run_context
        raise ContentFilterError("blocked by provider policy")
        yield  # pragma: no cover


async def test_pydantic_driver_translates_content_filter_without_inventing_response() -> None:
    driver = PydanticAIModelDriver(_FilteredTestModel())
    async with driver:
        async with driver.open_stream(_request()) as stream:
            events = [event async for event in stream.events]
            response = stream.response

    assert isinstance(events[-1], ModelResponseCompleted)
    assert events[-1].stop_reason is ModelStopReason.CONTENT_FILTER
    assert response is None
