from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, ThinkingPart
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from lumen.agent_loop import PydanticAIModelDriver
from lumen.config import LimitsConfig
from lumen.events import ProgressReported, RunEvent, ThinkingDelta, ToolCallFinished, ToolCallStarted
from lumen.runtime import AgentRuntime


@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("cancel_during_preparation", [False, True])
async def test_tool_preparation_is_visible_before_arguments_complete(
    thinking: bool, cancel_during_preparation: bool,
) -> None:
    events: list[RunEvent] = []
    executed: list[str] = []
    prepared = asyncio.Event()
    release_arguments = asyncio.Event()
    requests = 0

    def inspect_sample(value: str) -> str:
        """Inspect a sample."""
        executed.append(value)
        return "checked"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal requests
        requests += 1
        if requests > 1:
            yield "done"
            return
        if thinking:
            yield {0: DeltaThinkingPart(content="sample reasoning")}
        yield {1: DeltaToolCall("inspect_sample", '{"value":"', tool_call_id="sample")}
        await release_arguments.wait()
        yield {1: DeltaToolCall(json_args='sample"}')}

    async def emit(event: RunEvent) -> None:
        events.append(event)
        if isinstance(event, ProgressReported) and "正在准备工具调用: inspect_sample" in event.summary:
            prepared.set()

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[Tool(inspect_sample)], toolsets=[],
        instructions="Inspect a sample.", limits=LimitsConfig(),
        tool_metadata={"inspect_sample": {"origin": "test", "risk": "read"}},
    )
    run = asyncio.create_task(runtime.run("Inspect", [], emit, lambda _: pytest.fail("no approval")))
    try:
        await asyncio.wait_for(prepared.wait(), timeout=5)
        assert not executed
        assert not any(isinstance(event, ToolCallStarted | ToolCallFinished) for event in events)
        assert any(isinstance(event, ThinkingDelta) for event in events) is thinking
        if cancel_during_preparation:
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
            assert not executed
            assert not any(isinstance(event, ToolCallStarted | ToolCallFinished) for event in events)
            return
        release_arguments.set()
        outcome = await asyncio.wait_for(run, timeout=5)
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    assert outcome.output == "done"
    assert executed == ["sample"]
    assert sum(isinstance(event, ToolCallStarted) for event in events) == 1
    assert sum(isinstance(event, ToolCallFinished) for event in events) == 1


def _responses_stream(*, call_tool: bool, raw_reasoning: bool) -> httpx.AsyncByteStream:
    response: dict[str, Any] = {
        "id": "response-1" if call_tool else "response-2", "object": "response", "created_at": 0,
        "model": "deepseek-v4-flash", "status": "in_progress", "output": [],
    }
    chunks: list[dict[str, Any]] = [{"type": "response.created", "response": response}]
    output: list[dict[str, Any]] = []
    if raw_reasoning:
        reasoning: dict[str, Any] = {"id": "reasoning-1", "type": "reasoning", "summary": []}
        chunks.append({"type": "response.output_item.added", "output_index": 0, "item": reasoning})
        for text in ("inspect ", "sample"):
            chunks.append({
                "type": "response.reasoning_text.delta", "output_index": 0, "item_id": "reasoning-1",
                "content_index": 0, "delta": text,
            })
        reasoning = {**reasoning, "content": [{"type": "reasoning_text", "text": "inspect sample"}]}
        chunks.append({"type": "response.output_item.done", "output_index": 0, "item": reasoning})
        output.append(reasoning)
    index = len(output)
    if call_tool:
        item: dict[str, Any] = {
            "id": "fc-1", "type": "function_call", "call_id": "sample", "name": "inspect_sample",
            "arguments": "", "status": "in_progress",
        }
        chunks.append({"type": "response.output_item.added", "output_index": index, "item": item})
        chunks.append({
            "type": "response.function_call_arguments.delta", "output_index": index,
            "item_id": "fc-1", "delta": "{}",
        })
        item = {**item, "arguments": "{}", "status": "completed"}
    else:
        item = {"id": "msg-1", "type": "message", "role": "assistant", "content": [],
                "status": "in_progress"}
        chunks.append({"type": "response.output_item.added", "output_index": index, "item": item})
        chunks.append({
            "type": "response.output_text.delta", "output_index": index, "item_id": "msg-1",
            "content_index": 0, "delta": "checked", "logprobs": [],
        })
        item = {**item, "content": [{"type": "output_text", "text": "checked", "annotations": []}],
                "status": "completed"}
    chunks.append({"type": "response.output_item.done", "output_index": index, "item": item})
    output.append(item)
    chunks.append({"type": "response.completed", "response": {
        **response, "status": "completed", "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 5 if raw_reasoning else 0}},
    }})

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for sequence, chunk in enumerate(chunks):
                yield ("data: " + json.dumps({**chunk, "sequence_number": sequence}) + "\n\n").encode()

    return Stream()


@pytest.mark.parametrize("thinking", [None, False, "low", "native"])
async def test_deepseek_responses_thinking_and_tool_results_round_trip(thinking: str | bool | None) -> None:
    bodies: list[dict[str, Any]] = []
    events: list[RunEvent] = []
    executed: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_responses_stream(
            call_tool=len(bodies) == 1, raw_reasoning=thinking is not False,
        ))

    def inspect_sample() -> str:
        """Inspect a sample."""
        executed.append(True)
        return "checked"

    async def emit(event: RunEvent) -> None:
        events.append(event)

    settings: dict[str, Any] = {} if thinking is None else {"thinking": thinking}
    if thinking == "native":
        settings = {"thinking": False, "openai_reasoning_effort": "high"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAIResponsesModel("deepseek-v4-flash", provider=OpenAIProvider(
            openai_client=AsyncOpenAI(
                api_key="test", base_url="https://api.deepseek.com", http_client=client,
            ),
        ))
        runtime = AgentRuntime(
            model=model, tools=[Tool(inspect_sample)], toolsets=[], instructions="Inspect a sample.",
            limits=LimitsConfig(request_count=2),
            model_settings=cast(ModelSettings, settings),
            tool_metadata={"inspect_sample": {"origin": "test", "risk": "read"}},
        )
        outcome = await runtime.run("Inspect", [], emit, lambda _: pytest.fail("no approval"))
    assert outcome.output == "checked"
    assert executed == [True]
    assert len(bodies) == 2
    for body in bodies:
        if thinking is None:
            assert "reasoning" not in body
        else:
            effort = "high" if thinking == "native" else ("none" if thinking is False else "low")
            assert body.get("reasoning") == {"effort": effort}
    assert sum(isinstance(event, ToolCallFinished) for event in events) == 1
    visible_thinking = "".join(event.text for event in events if isinstance(event, ThinkingDelta))
    assert visible_thinking == ("" if thinking is False else "inspect sampleinspect sample")
    replayed = [item for item in bodies[1]["input"] if item.get("type") == "reasoning"]
    if thinking is False:
        assert not replayed
    else:
        assert replayed[0]["content"] == [{"type": "reasoning_text", "text": "inspect sample"}]
        restored = ModelMessagesTypeAdapter.validate_json(
            ModelMessagesTypeAdapter.dump_json(outcome.new_messages),
        )
        driver = PydanticAIModelDriver("test")
        assert "".join(driver.response_thinking(message) for message in restored) == visible_thinking


def test_response_thinking_includes_raw_text_without_exposing_opaque_metadata() -> None:
    response = ModelResponse(parts=[ThinkingPart(
        content="summary", signature="opaque-signature", provider_details={
            "raw_content": ["raw ", "text", 42], "other": "opaque-data",
        },
    )])
    # Canonical provider details remain available for replay; only text is projected.
    assert PydanticAIModelDriver("test").response_thinking(response) == "raw textsummary"
