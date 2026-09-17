"""Bounded interactive input queue consumed by the native agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from lumen.attachments import AttachmentRef


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
    attachments: tuple[AttachmentRef, ...] = ()


class InteractiveMessageQueue:
    """Own pending user messages until they cross into an active agent run."""

    def __init__(self, *, max_messages: int = 50, max_bytes: int = 256 * 1024) -> None:
        if max_messages < 1 or max_bytes < 1:
            raise ValueError("queue limits must be positive")
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self._messages: list[QueuedMessage] = []

    def enqueue(
        self,
        text: str,
        model_prompt: str,
        mode: QueueMode | str,
        attachments: tuple[AttachmentRef, ...] = (),
    ) -> QueuedMessage:
        parsed = mode if isinstance(mode, QueueMode) else QueueMode(mode)
        size = len(model_prompt.encode("utf-8"))
        if len(self._messages) >= self.max_messages:
            raise QueueLimitError(f"interactive queue is limited to {self.max_messages} messages")
        if sum(item.byte_size for item in self._messages) + size > self.max_bytes:
            raise QueueLimitError(f"interactive queue is limited to {self.max_bytes} bytes")
        message = QueuedMessage(str(uuid4()), text, model_prompt, parsed, size, attachments)
        self._messages.append(message)
        return message

    def dequeue_all(self) -> tuple[QueuedMessage, ...]:
        messages = tuple(self._messages)
        self._messages.clear()
        return messages

    def dequeue_mode(
        self,
        mode: QueueMode,
        *,
        limit: int | None = None,
    ) -> tuple[QueuedMessage, ...]:
        """Atomically take matching messages while preserving queue order."""

        selected: list[QueuedMessage] = []
        retained: list[QueuedMessage] = []
        for message in self._messages:
            if message.mode is mode and (limit is None or len(selected) < limit):
                selected.append(message)
            else:
                retained.append(message)
        self._messages = retained
        return tuple(selected)

    def snapshot(self) -> tuple[QueuedMessage, ...]:
        return tuple(self._messages)

__all__ = [
    "InteractiveMessageQueue",
    "QueueLimitError",
    "QueueMode",
    "QueuedMessage",
]
