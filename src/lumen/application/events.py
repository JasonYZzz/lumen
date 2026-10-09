from __future__ import annotations

import asyncio
import json
import tempfile
from collections import deque
from collections.abc import AsyncGenerator
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, cast

from pydantic_core import to_jsonable_python

from lumen.approval import ApprovalPresenter
from lumen.events import (
    AgentLifecycleChanged,
    ApprovalRequest,
    ClarificationRequested,
    CommentaryDelta,
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    InputDelivered,
    InputDequeued,
    InputQueued,
    PlanCreated,
    PlanReviewPending,
    PlanReviewResolved,
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
    ToolApprovalBatchPending,
    ToolApprovalPending,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
    WorkProductChanged,
)
from lumen.io import run_io
from lumen.plan import PlanState

from .models import EventEnvelope

_EVENT_NAMES: dict[type[object], str] = {
    AgentLifecycleChanged: "agent.lifecycle",
    RunStarted: "run.started",
    TextDelta: "assistant.delta",
    TextRetracted: "assistant.retracted",
    CommentaryDelta: "commentary.delta",
    ThinkingDelta: "thinking.delta",
    PlanCreated: "plan.created",
    PlanUpdated: "plan.updated",
    PlanReviewPending: "plan.review_pending",
    PlanReviewResolved: "plan.review_resolved",
    ProgressReported: "progress.reported",
    WorkProductChanged: "work_product.changed",
    ToolCallStarted: "tool.started",
    ToolCallFinished: "tool.finished",
    ToolApprovalPending: "approval.pending",
    ToolApprovalBatchPending: "approval.batch_pending",
    ToolApprovalResolved: "approval.resolved",
    UsageUpdated: "usage.updated",
    ContextCompactionStarted: "context.compaction.started",
    ContextCompactionCompleted: "context.compaction.completed",
    ContextCompactionFailed: "context.compaction.failed",
    RunCompleted: "run.completed",
    ClarificationRequested: "clarification.requested",
    RunWaitingForUser: "run.waiting_for_user",
    RunFailed: "run.failed",
    RunCancelled: "run.cancelled",
    InputQueued: "input.queued",
    InputDelivered: "input.delivered",
    InputDequeued: "input.dequeued",
}
_EVENT_TYPES = {name: event_type for event_type, name in _EVENT_NAMES.items()}


def event_payload(event: RunEvent) -> tuple[str, dict[str, Any]]:
    name = _EVENT_NAMES[type(event)]
    if isinstance(event, TextDelta | CommentaryDelta | ThinkingDelta):
        key = "text"
        value = event.text
        return name, {key: value}
    if isinstance(event, TextRetracted):
        return name, {"characters": event.characters}
    data = asdict(event)
    if isinstance(event, RunStarted):
        data["attachments"] = [item.model_dump(mode="json") for item in event.attachments]
    if isinstance(event, PlanCreated | PlanUpdated | PlanReviewPending):
        data["plan"] = event.plan.model_dump(mode="json")
    if isinstance(event, ToolApprovalPending):
        presenter = ApprovalPresenter().build(
            request=ApprovalRequest(
                call_id=event.call_id,
                name=event.name,
                args=event.args,
                origin=event.origin,
                risk=event.risk,
            )
        )
        data["presentation"] = asdict(presenter)
    elif isinstance(event, ToolApprovalBatchPending):
        data["presentations"] = [
            asdict(ApprovalPresenter().build(request=request)) for request in event.requests
        ]
    return name, data


def event_from_payload(name: str, payload: dict[str, Any]) -> RunEvent:
    """Rehydrate a Host event for in-process clients such as the TUI."""

    event_type = _EVENT_TYPES.get(name)
    if event_type is None:
        raise ValueError(f"unknown host event type: {name}")
    data = dict(payload)
    data.pop("presentation", None)
    data.pop("presentations", None)
    if event_type is RunStarted:
        from lumen.attachments import AttachmentRef

        data["attachments"] = tuple(
            AttachmentRef.model_validate(item) for item in data.get("attachments", [])
        )
    if event_type in {PlanCreated, PlanUpdated, PlanReviewPending}:
        data["plan"] = PlanState.model_validate(data["plan"])
    elif event_type is ToolApprovalBatchPending:
        data["requests"] = tuple(ApprovalRequest(**item) for item in data["requests"])
    return cast(RunEvent, event_type(**data))


class EventJournal:
    """Replayable, connection-independent event stream for one run."""

    def __init__(self, *, session_id: str, run_id: str, max_bytes: int = 1024 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("event replay memory budget must be positive")
        self.session_id = session_id
        self.run_id = run_id
        self._events: deque[tuple[EventEnvelope, int]] = deque()
        self._max_bytes = max_bytes
        self.retained_bytes = 0
        self.subscriber_count = 0
        self._sequence = 0
        # The transient replay file is a display projection, never a Session
        # authority. It is deleted when the Host releases this run.
        self._replay = tempfile.TemporaryFile(mode="w+b")
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return tuple(event for event, _ in self._events)

    async def append(self, event: RunEvent) -> EventEnvelope:
        name, data = event_payload(event)
        cancelled: asyncio.CancelledError | None = None
        async with self._condition:
            envelope = EventEnvelope(
                sequence=self._sequence + 1,
                session_id=self.session_id,
                run_id=self.run_id,
                type=name,
                data=cast(dict[str, Any], to_jsonable_python(data)),
            )
            encoded = (
                json.dumps(to_jsonable_python(asdict(envelope)), ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode()
            def write() -> None:
                self._replay.seek(0, 2)
                offset = self._replay.tell()
                try:
                    self._replay.write(encoded)
                    self._replay.flush()
                except BaseException:
                    self._replay.seek(offset)
                    self._replay.truncate()
                    raise
            try:
                await run_io(write)
            except asyncio.CancelledError as error:
                cancelled = error
            self._sequence = envelope.sequence
            self._events.append((envelope, len(encoded)))
            self.retained_bytes += len(encoded)
            while self.retained_bytes > self._max_bytes:
                _, size = self._events.popleft()
                self.retained_bytes -= size
            self._condition.notify_all()
        if cancelled is not None and not isinstance(
            event, RunCompleted | RunWaitingForUser | RunFailed | RunCancelled,
        ):
            raise cancelled
        return envelope

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def subscribe(self, after_sequence: int | None = None) -> AsyncGenerator[EventEnvelope, None]:
        sequence = max(0, after_sequence or 0)
        offset = 0
        self.subscriber_count += 1
        try:
            while True:
                async with self._condition:
                    await self._condition.wait_for(
                        lambda position=sequence: position < self._sequence or self._closed
                    )
                    if sequence >= self._sequence:
                        if self._closed:
                            return
                        continue
                    def read(offset: int = offset, sequence: int = sequence) -> tuple[EventEnvelope, int]:
                        # ponytail: cold reconnect scans up to its cursor once;
                        # add a sparse disk offset index only if measured slow.
                        self._replay.seek(offset)
                        while line := self._replay.readline():
                            item = EventEnvelope(**json.loads(line))
                            if item.sequence > sequence:
                                return item, self._replay.tell()
                        raise RuntimeError("event replay ended before its published sequence")
                    if self._events and sequence >= self._events[0][0].sequence - 1:
                        event = self._events[sequence + 1 - self._events[0][0].sequence][0]
                    else:
                        event, offset = await run_io(read)
                    sequence = event.sequence
                yield replace(event, data=deepcopy(event.data))
        finally:
            self.subscriber_count -= 1

    def dispose(self) -> None:
        if not self._closed or self.subscriber_count:
            raise RuntimeError("cannot release an active or subscribed event journal")
        self._replay.close()
        self._events.clear()
        self.retained_bytes = 0
