from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import LimitsConfig
from lumen.events import InputDelivered, RunEvent
from lumen.interactive_queue import (
    InteractiveMessageQueue,
    QueueLimitError,
    QueueMode,
)
from lumen.runtime import AgentRuntime, ToolApproval


def test_interactive_queue_is_fifo_and_dequeues_pending_messages() -> None:
    queue = InteractiveMessageQueue()
    first = queue.enqueue("first", "expanded first", QueueMode.STEER)
    second = queue.enqueue("second", "expanded second", QueueMode.FOLLOW_UP)

    assert queue.snapshot() == (first, second)
    assert queue.dequeue_all() == (first, second)
    assert queue.snapshot() == ()


def test_interactive_queue_dequeues_one_mode_without_reordering_the_rest() -> None:
    queue = InteractiveMessageQueue()
    steer_one = queue.enqueue("steer one", "one", QueueMode.STEER)
    follow_up = queue.enqueue("follow", "later", QueueMode.FOLLOW_UP)
    steer_two = queue.enqueue("steer two", "two", QueueMode.STEER)

    assert queue.dequeue_mode(QueueMode.STEER) == (steer_one, steer_two)
    assert queue.snapshot() == (follow_up,)
    assert queue.dequeue_mode(QueueMode.FOLLOW_UP, limit=1) == (follow_up,)


def test_interactive_queue_enforces_count_and_byte_limits() -> None:
    queue = InteractiveMessageQueue(max_messages=1, max_bytes=8)
    queue.enqueue("one", "12345678", QueueMode.STEER)

    with pytest.raises(QueueLimitError, match=r"50|1"):
        queue.enqueue("two", "x", QueueMode.STEER)

    byte_limited = InteractiveMessageQueue(max_messages=50, max_bytes=4)
    with pytest.raises(QueueLimitError, match="bytes"):
        byte_limited.enqueue("large", "12345", QueueMode.STEER)


async def test_runtime_delivers_steering_at_next_model_boundary() -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    model_saw_steering = False

    async def pause_tool() -> str:
        tool_started.set()
        await release_tool.wait()
        return "tool done"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal model_saw_steering
        has_return = any(
            isinstance(part, ToolReturnPart)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_return:
            yield {0: DeltaToolCall("pause_tool", "{}", tool_call_id="call-1")}
            return
        prompts = [
            str(part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        model_saw_steering = any("steer injected" in prompt for prompt in prompts)
        yield "steering received"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(pause_tool, name="pause_tool")],
        toolsets=[],
        instructions="test",
        limits=LimitsConfig(),
        tool_metadata={"pause_tool": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: object) -> ToolApproval:
        return ToolApproval(True)

    task = asyncio.create_task(runtime.run("start", [], emit, approve))
    await tool_started.wait()
    queued = runtime.interactive_queue.enqueue(
        "literal steer",
        "steer injected",
        QueueMode.STEER,
    )
    release_tool.set()
    outcome = await task

    assert outcome.output == "steering received"
    assert model_saw_steering is True
    assert any(isinstance(event, InputDelivered) and event.message_id == queued.id for event in events)
