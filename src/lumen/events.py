from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, TypeAlias, cast

from pydantic import BaseModel

from lumen.plan import PlanState


@dataclass(frozen=True, slots=True)
class ToolExecutionDiagnostic:
    call_id: str
    name: str
    status: str
    error_category: str | None = None
    exit_code: int | None = None
    elapsed_seconds: float = 0.0
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunStarted:
    prompt: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Incremental assistant text, emitted as soon as the provider yields it."""

    text: str


@dataclass(frozen=True, slots=True)
class TextRetracted:
    """Retract provisional assistant text before reclassifying it."""

    characters: int


@dataclass(frozen=True, slots=True)
class CommentaryDelta:
    """Intermediate text the model produced alongside tool calls.

    Providers may emit this directly after a tool call, or the runtime may
    reclassify already-streamed provisional text using :class:`TextRetracted`.
    It is shown as a subdued analysis summary in the timeline.
    """

    text: str


@dataclass(frozen=True, slots=True)
class PlanCreated:
    plan: PlanState


@dataclass(frozen=True, slots=True)
class PlanUpdated:
    plan: PlanState


@dataclass(frozen=True, slots=True)
class ProgressReported:
    summary: str
    next_action: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    call_id: str
    name: str
    args: dict[str, Any]
    origin: str = "configured tool"
    risk: str = "external"
    started_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    call_id: str
    name: str
    result: str
    is_error: bool
    elapsed_seconds: float = 0.0
    preview: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class ToolApprovalPending:
    call_id: str
    name: str
    args: dict[str, Any]
    origin: str
    risk: str


@dataclass(frozen=True, slots=True)
class ToolApprovalBatchPending:
    """One model turn produced several approval-gated tool calls."""

    batch_id: str
    requests: tuple[ApprovalRequest, ...]
    risk_summary: str


@dataclass(frozen=True, slots=True)
class ToolApprovalResolved:
    call_id: str
    approved: bool
    message: str


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Approval request details for backwards compatibility with callbacks."""

    call_id: str
    name: str
    args: dict[str, Any]
    origin: str
    risk: str


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    """Legacy alias for ToolApprovalPending for compatibility."""

    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class UsageUpdated:
    usage: dict[str, Any]
    request_count: int = 0
    tool_call_count: int = 0
    context_tokens_estimate: int = 0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ContextCompactionStarted:
    source_message_count: int


@dataclass(frozen=True, slots=True)
class ContextCompactionCompleted:
    active_message_count: int
    summary_tokens_estimate: int = 0


@dataclass(frozen=True, slots=True)
class ContextCompactionFailed:
    message: str


@dataclass(frozen=True, slots=True)
class RunCompleted:
    output: str
    usage: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class ClarificationRequested:
    question_id: str
    question: str
    choices: tuple[str, ...] = ()
    related_plan_step: str | None = None


@dataclass(frozen=True, slots=True)
class RunWaitingForUser:
    question_id: str
    question: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunFailed:
    message: str


@dataclass(frozen=True, slots=True)
class RunCancelled:
    message: str = "Run cancelled"


@dataclass(frozen=True, slots=True)
class InputQueued:
    message_id: str
    text: str
    mode: str


@dataclass(frozen=True, slots=True)
class InputDelivered:
    message_id: str
    text: str
    mode: str


@dataclass(frozen=True, slots=True)
class InputDequeued:
    message_id: str
    text: str
    mode: str


RunEvent: TypeAlias = (
    RunStarted
    | TextDelta
    | TextRetracted
    | CommentaryDelta
    | PlanCreated
    | PlanUpdated
    | ProgressReported
    | ToolCallStarted
    | ToolCallFinished
    | ToolApprovalPending
    | ToolApprovalBatchPending
    | ToolApprovalResolved
    | ApprovalRequested
    | UsageUpdated
    | ContextCompactionStarted
    | ContextCompactionCompleted
    | ContextCompactionFailed
    | RunCompleted
    | ClarificationRequested
    | RunWaitingForUser
    | RunFailed
    | RunCancelled
    | InputQueued
    | InputDelivered
    | InputDequeued
)


@dataclass(frozen=True, slots=True)
class TimelineEventRecord:
    """Version-stable, tagged representation of one public run event."""

    type: str
    data: dict[str, Any]
    sequence: int

    @classmethod
    def from_event(cls, event: RunEvent, *, sequence: int) -> TimelineEventRecord:
        raw = asdict(event)
        return cls(type=type(event).__name__, data=_jsonable(raw), sequence=sequence)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TimelineEventRecord:
        return cls(
            type=str(value["type"]),
            data=dict(value.get("data") or {}),
            sequence=int(value["sequence"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "sequence": self.sequence}

    def to_event(self) -> RunEvent:
        event_type = _EVENT_TYPES.get(self.type)
        if event_type is None:
            raise ValueError(f"unknown timeline event type: {self.type}")
        data = dict(self.data)
        if event_type in {PlanCreated, PlanUpdated}:
            data["plan"] = PlanState.model_validate(data["plan"])
        elif event_type is ToolApprovalBatchPending:
            data["requests"] = tuple(ApprovalRequest(**item) for item in data["requests"])
        elif event_type is ApprovalRequested:
            data["request"] = ApprovalRequest(**data["request"])
        return event_type(**data)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in cast(list[object] | tuple[object, ...], value)]
    return value


_EVENT_TYPES: dict[str, type[Any]] = {
    event_type.__name__: event_type
    for event_type in (
        RunStarted,
        TextDelta,
        TextRetracted,
        CommentaryDelta,
        PlanCreated,
        PlanUpdated,
        ProgressReported,
        ToolCallStarted,
        ToolCallFinished,
        ToolApprovalPending,
        ToolApprovalBatchPending,
        ToolApprovalResolved,
        ApprovalRequested,
        UsageUpdated,
        ContextCompactionStarted,
        ContextCompactionCompleted,
        ContextCompactionFailed,
        RunCompleted,
        ClarificationRequested,
        RunWaitingForUser,
        RunFailed,
        RunCancelled,
        InputQueued,
        InputDelivered,
        InputDequeued,
    )
}
