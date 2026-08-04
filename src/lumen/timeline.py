"""Structured, bounded timeline state independent of Textual widgets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from lumen.events import (
    ClarificationRequested,
    CommentaryDelta,
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
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
    ToolApprovalBatchPending,
    ToolApprovalPending,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
)
from lumen.sessions import SessionRepository, TurnRecord

_CONTROL_TOOL_NAMES = frozenset(
    {"set_plan", "update_step", "report_progress", "request_clarification"}
)


class TimelineKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    PLAN = "plan"
    COMMENTARY = "commentary"
    PROGRESS = "progress"
    TOOL = "tool"
    SYSTEM = "system"
    ERROR = "error"
    COMPACTION = "compaction"


@dataclass(frozen=True, slots=True)
class TimelineItem:
    id: str
    kind: TimelineKind
    text: str = ""
    call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: str | None = None
    preview: str | None = None
    status: str | None = None
    pending_approval: bool = False
    is_error: bool = False
    plan: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TimelinePage:
    items: list[TimelineItem]
    next_cursor: int | None


class TimelineAdapter(Protocol):
    def load_page(self, cursor: int | None, *, limit: int) -> TimelinePage: ...


class InMemoryTimelineAdapter:
    """Empty/default adapter, also useful for event-only tests."""

    def __init__(self, pages: dict[int | None, TimelinePage] | None = None) -> None:
        self._pages = dict(pages or {})

    def load_page(self, cursor: int | None, *, limit: int) -> TimelinePage:
        page = self._pages.get(cursor, TimelinePage([], None))
        return TimelinePage(page.items[-limit:], page.next_cursor)


class RepositoryTimelineAdapter:
    def __init__(self, repository: SessionRepository, session_id: str) -> None:
        self._repository = repository
        self._session_id = session_id

    def load_page(self, cursor: int | None, *, limit: int) -> TimelinePage:
        page = self._repository.load_turn_page(self._session_id, before=cursor, limit=limit)
        items: list[TimelineItem] = []
        for turn_index, turn in enumerate(page.turns):
            items.extend(_turn_items(turn, ordinal=(page.next_before or 0) + turn_index))
        return TimelinePage(items, page.next_before)


class TimelineStore:
    """Own timeline lifecycle, pagination, coalescing and bounded views."""

    def __init__(self, adapter: TimelineAdapter | None = None) -> None:
        self._adapter = adapter or InMemoryTimelineAdapter()
        self._items: list[TimelineItem] = []
        self._tool_indexes: dict[str, int] = {}
        self._sequence = 0
        self._next_cursor: int | None = None

    @property
    def next_cursor(self) -> int | None:
        return self._next_cursor

    def apply(self, event: RunEvent) -> TimelineItem | None:
        item: TimelineItem | None = None
        if isinstance(event, RunStarted):
            item = self._new(TimelineKind.USER, text=event.prompt)
        elif isinstance(event, TextDelta):
            if self._items and self._items[-1].kind is TimelineKind.ASSISTANT:
                item = replace(self._items[-1], text=self._items[-1].text + event.text)
                self._items[-1] = item
                return item
            item = self._new(TimelineKind.ASSISTANT, text=event.text)
        elif isinstance(event, TextRetracted):
            if not self._items or self._items[-1].kind is not TimelineKind.ASSISTANT:
                return None
            previous = self._items[-1].text
            remaining = previous[: -event.characters] if event.characters else previous
            if remaining:
                item = replace(self._items[-1], text=remaining)
                self._items[-1] = item
                return item
            self._items.pop()
            return None
        elif isinstance(event, CommentaryDelta):
            if self._items and self._items[-1].kind is TimelineKind.COMMENTARY:
                item = replace(self._items[-1], text=self._items[-1].text + event.text)
                self._items[-1] = item
                return item
            item = self._new(TimelineKind.COMMENTARY, text=event.text)
        elif isinstance(event, PlanCreated | PlanUpdated):
            plan = event.plan.model_dump(mode="json")
            for index in range(len(self._items) - 1, -1, -1):
                existing = self._items[index]
                if existing.kind is TimelineKind.USER:
                    break
                if existing.kind is TimelineKind.PLAN:
                    item = replace(existing, plan=plan)
                    self._items[index] = item
                    return item
            item = self._new(TimelineKind.PLAN, plan=plan)
        elif isinstance(event, ProgressReported):
            text = event.summary + (f"\n{event.next_action}" if event.next_action else "")
            item = self._new(TimelineKind.PROGRESS, text=text)
        elif isinstance(event, ToolCallStarted):
            if event.origin == "control" or event.name in _CONTROL_TOOL_NAMES:
                return None
            item = self._new(
                TimelineKind.TOOL,
                call_id=event.call_id,
                tool_name=event.name,
                args=event.args,
                status="running",
            )
        elif isinstance(event, ToolCallFinished):
            return self._update_tool(
                event.call_id,
                result=event.result,
                preview=event.preview,
                status="error" if event.is_error else "ok",
                is_error=event.is_error,
                pending_approval=False,
            )
        elif isinstance(event, ToolApprovalPending):
            existing = self._tool(event.call_id)
            if existing is not None:
                return self._update_tool(
                    event.call_id,
                    args=event.args,
                    status="pending",
                    pending_approval=True,
                )
            item = self._new(
                TimelineKind.TOOL,
                call_id=event.call_id,
                tool_name=event.name,
                args=event.args,
                status="pending",
                pending_approval=True,
            )
        elif isinstance(event, ToolApprovalBatchPending):
            first: TimelineItem | None = None
            for request in event.requests:
                current = self.apply(
                    ToolApprovalPending(
                        request.call_id,
                        request.name,
                        request.args,
                        request.origin,
                        request.risk,
                    )
                )
                first = first or current
            return first
        elif isinstance(event, ToolApprovalResolved):
            return self._update_tool(
                event.call_id,
                status="approved" if event.approved else "denied",
                pending_approval=False,
            )
        elif isinstance(event, ContextCompactionStarted):
            item = self._new(TimelineKind.COMPACTION, text="Compacting context…")
        elif isinstance(event, ContextCompactionCompleted):
            item = self._new(
                TimelineKind.COMPACTION,
                text=f"Context compacted: {event.active_message_count} active messages.",
            )
        elif isinstance(event, ContextCompactionFailed):
            item = self._new(TimelineKind.ERROR, text=event.message, is_error=True)
        elif isinstance(event, RunFailed):
            item = self._new(TimelineKind.ERROR, text=event.message, is_error=True)
        elif isinstance(event, RunCancelled):
            item = self._new(TimelineKind.SYSTEM, text=event.message)
        elif isinstance(event, ClarificationRequested):
            choices = "\n".join(f"- {choice}" for choice in event.choices)
            text = event.question + (f"\n{choices}" if choices else "")
            item = self._new(TimelineKind.SYSTEM, text=text, status="waiting_for_user")
        elif isinstance(event, RunCompleted | RunWaitingForUser):
            return None
        if item is not None:
            self._append(item)
        return item

    def window(self, limit: int = 200) -> list[TimelineItem]:
        if limit < 1:
            raise ValueError("timeline window limit must be positive")
        return list(self._items[-limit:])

    def load_older(self, cursor: int | None = None, *, limit: int = 20) -> TimelinePage:
        page = self._adapter.load_page(cursor, limit=limit)
        known = {item.id for item in self._items}
        older = [item for item in page.items if item.id not in known]
        self._items[0:0] = older
        self._next_cursor = page.next_cursor
        self._reindex_tools()
        return TimelinePage(older, page.next_cursor)

    def _new(self, kind: TimelineKind, **values: Any) -> TimelineItem:
        self._sequence += 1
        return TimelineItem(id=f"live:{self._sequence}", kind=kind, **values)

    def _append(self, item: TimelineItem) -> None:
        self._items.append(item)
        if item.call_id:
            self._tool_indexes[item.call_id] = len(self._items) - 1

    def _tool(self, call_id: str) -> TimelineItem | None:
        index = self._tool_indexes.get(call_id)
        return self._items[index] if index is not None else None

    def _update_tool(self, call_id: str, **changes: Any) -> TimelineItem | None:
        index = self._tool_indexes.get(call_id)
        if index is None:
            return None
        item = replace(self._items[index], **changes)
        self._items[index] = item
        return item

    def _reindex_tools(self) -> None:
        self._tool_indexes = {
            item.call_id: index for index, item in enumerate(self._items) if item.call_id is not None
        }


def _turn_items(turn: TurnRecord, *, ordinal: int) -> list[TimelineItem]:
    prefix = f"turn:{ordinal}:{turn.created_at}"
    if turn.timeline_events:
        replay = TimelineStore()
        for record in sorted(turn.timeline_events, key=lambda item: item.sequence):
            replay.apply(record.to_event())
        items = [
            replace(item, id=f"{prefix}:event:{index}")
            for index, item in enumerate(replay.window(limit=max(1, len(turn.timeline_events))), 1)
        ]
        # Eventful turns are authoritative: only an actual PlanCreated or
        # PlanUpdated event makes a plan belong to this turn. ``turn.plan`` is
        # the session's latest diagnostic snapshot and may be inherited from
        # an earlier run, so using it here duplicates stale plans after reload.
        return items
    items = [TimelineItem(f"{prefix}:user", TimelineKind.USER, text=turn.user_input)]
    if turn.plan.steps:
        items.append(
            TimelineItem(
                f"{prefix}:plan",
                TimelineKind.PLAN,
                plan=turn.plan.model_dump(mode="json"),
            )
        )
    assistant_parts: list[str] = []
    tool_number = 0
    tool_positions: dict[str, int] = {}
    for message in turn.messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, TextPart):
                    assistant_parts.append(part.content)
                elif isinstance(part, ToolCallPart):
                    if part.tool_name in _CONTROL_TOOL_NAMES:
                        continue
                    tool_number += 1
                    items.append(
                        TimelineItem(
                            f"{prefix}:tool:{tool_number}",
                            TimelineKind.TOOL,
                            call_id=part.tool_call_id,
                            tool_name=part.tool_name,
                            args=part.args_as_dict(),
                            status="completed",
                        )
                    )
                    tool_positions[part.tool_call_id] = len(items) - 1
        else:
            for part in message.parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                if part.tool_name in _CONTROL_TOOL_NAMES:
                    continue
                result = str(part.content)
                position = tool_positions.get(part.tool_call_id)
                if position is None:
                    tool_number += 1
                    items.append(
                        TimelineItem(
                            f"{prefix}:tool:{tool_number}",
                            TimelineKind.TOOL,
                            call_id=part.tool_call_id,
                            tool_name=part.tool_name,
                            result=result,
                            preview=result[:200],
                            status="ok",
                        )
                    )
                else:
                    items[position] = replace(
                        items[position],
                        result=result,
                        preview=result[:200],
                        status="ok",
                    )
    text = "".join(assistant_parts) or turn.partial_text
    if text:
        items.append(TimelineItem(f"{prefix}:assistant", TimelineKind.ASSISTANT, text=text))
    if turn.status in {"failed", "cancelled"}:
        message = turn.error_message or ("Run cancelled" if turn.status == "cancelled" else "Run failed")
        kind = TimelineKind.ERROR if turn.status == "failed" else TimelineKind.SYSTEM
        items.append(
            TimelineItem(
                f"{prefix}:{turn.status}",
                kind,
                text=message,
                is_error=turn.status == "failed",
            )
        )
    return items


__all__ = [
    "InMemoryTimelineAdapter",
    "RepositoryTimelineAdapter",
    "TimelineAdapter",
    "TimelineItem",
    "TimelineKind",
    "TimelinePage",
    "TimelineStore",
]
