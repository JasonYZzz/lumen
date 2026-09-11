from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, cast

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

    def __init__(self, *, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self._events: list[EventEnvelope] = []
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return tuple(self._events)

    async def append(self, event: RunEvent) -> EventEnvelope:
        name, data = event_payload(event)
        async with self._condition:
            envelope = EventEnvelope(
                sequence=len(self._events) + 1,
                session_id=self.session_id,
                run_id=self.run_id,
                type=name,
                data=data,
            )
            self._events.append(envelope)
            self._condition.notify_all()
            return envelope

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def subscribe(self, after_sequence: int | None = None) -> AsyncIterator[EventEnvelope]:
        index = max(0, after_sequence or 0)
        while True:
            async with self._condition:
                await self._condition.wait_for(
                    lambda position=index: position < len(self._events) or self._closed
                )
                pending = self._events[index:]
                closed = self._closed
            for event in pending:
                index = event.sequence
                yield event
            if closed and index >= len(self._events):
                return
