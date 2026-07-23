"""Bounded interactive input queue and Pydantic AI delivery capability."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import RunContext

from lumen.events import InputDelivered, RunEvent


class QueueMode(StrEnum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"


class QueueLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    id: str
    text: str
    model_prompt: str
    mode: QueueMode
    byte_size: int


class InteractiveMessageQueue:
    """Own pending user messages until they cross into an active agent run."""

    def __init__(self, *, max_messages: int = 50, max_bytes: int = 256 * 1024) -> None:
        if max_messages < 1 or max_bytes < 1:
            raise ValueError("queue limits must be positive")
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self._messages: list[QueuedMessage] = []

    def enqueue(self, text: str, model_prompt: str, mode: QueueMode | str) -> QueuedMessage:
        parsed = mode if isinstance(mode, QueueMode) else QueueMode(mode)
        size = len(model_prompt.encode("utf-8"))
        if len(self._messages) >= self.max_messages:
            raise QueueLimitError(f"interactive queue is limited to {self.max_messages} messages")
        if sum(item.byte_size for item in self._messages) + size > self.max_bytes:
            raise QueueLimitError(f"interactive queue is limited to {self.max_bytes} bytes")
        message = QueuedMessage(str(uuid4()), text, model_prompt, parsed, size)
        self._messages.append(message)
        return message

    def dequeue_all(self) -> tuple[QueuedMessage, ...]:
        messages = tuple(self._messages)
        self._messages.clear()
        return messages

    def snapshot(self) -> tuple[QueuedMessage, ...]:
        return tuple(self._messages)

    def mark_delivered(self, message_id: str) -> None:
        self._messages = [message for message in self._messages if message.id != message_id]


class InteractiveInputCapability(AbstractCapability[None]):
    """Deliver queued input through Pydantic AI's supported enqueue seam."""

    def __init__(
        self,
        queue: InteractiveMessageQueue,
        emit: Callable[[RunEvent], Awaitable[None]],
    ) -> None:
        self.queue = queue
        self.emit = emit

    async def before_node_run(self, ctx: RunContext[None], *, node: object) -> object:  # type: ignore[override]
        steering = [message for message in self.queue.snapshot() if message.mode is QueueMode.STEER]
        for message in steering:
            ctx.enqueue(message.model_prompt, priority="asap")
            self.queue.mark_delivered(message.id)
            await self.emit(InputDelivered(message.id, message.text, message.mode.value))

        has_follow_up = any(
            getattr(message, "priority", None) == "when_idle" for message in (ctx.pending_messages or [])
        )
        if not has_follow_up:
            follow_ups = [message for message in self.queue.snapshot() if message.mode is QueueMode.FOLLOW_UP]
            if follow_ups:
                message = follow_ups[0]
                ctx.enqueue(message.model_prompt, priority="when_idle")
                self.queue.mark_delivered(message.id)
                await self.emit(InputDelivered(message.id, message.text, message.mode.value))
        return node


__all__ = [
    "InteractiveInputCapability",
    "InteractiveMessageQueue",
    "QueueLimitError",
    "QueueMode",
    "QueuedMessage",
]
