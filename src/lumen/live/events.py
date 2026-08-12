"""Bounded replayable event journal for an active Live session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from lumen.live.types import LiveEvent, LiveEventKind


class LiveEventJournal:
    def __init__(self, *, live_session_id: str, session_id: str, max_events: int = 2_000) -> None:
        self.live_session_id = live_session_id
        self.session_id = session_id
        self.max_events = max_events
        self._events: list[LiveEvent] = []
        self._next_sequence = 1
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def events(self) -> tuple[LiveEvent, ...]:
        return tuple(self._events)

    async def append(self, kind: LiveEventKind, data: dict[str, Any] | None = None) -> LiveEvent:
        async with self._condition:
            event = LiveEvent(
                sequence=self._next_sequence,
                live_session_id=self.live_session_id,
                session_id=self.session_id,
                kind=kind,
                data=data or {},
            )
            self._next_sequence += 1
            self._events.append(event)
            if len(self._events) > self.max_events:
                del self._events[: len(self._events) - self.max_events]
            self._condition.notify_all()
            return event

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def subscribe(self, after_sequence: int | None = None) -> AsyncIterator[LiveEvent]:
        cursor = after_sequence or 0
        while True:
            async with self._condition:
                await self._condition.wait_for(
                    lambda position=cursor: (
                        any(item.sequence > position for item in self._events) or self._closed
                    )
                )
                pending = [item for item in self._events if item.sequence > cursor]
                closed = self._closed
            for event in pending:
                cursor = event.sequence
                yield event
            if closed and not any(item.sequence > cursor for item in self._events):
                return


__all__ = ["LiveEventJournal"]
