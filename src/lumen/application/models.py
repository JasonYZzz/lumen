from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeAlias

from lumen.timeline import TimelineItem


@dataclass(frozen=True, slots=True)
class CreateSession:
    type: Literal["create_session"] = "create_session"


@dataclass(frozen=True, slots=True)
class GetBootstrap:
    type: Literal["get_bootstrap"] = "get_bootstrap"


@dataclass(frozen=True, slots=True)
class ListSessions:
    type: Literal["list_sessions"] = "list_sessions"


@dataclass(frozen=True, slots=True)
class StartRun:
    session_id: str
    input: str
    client_request_id: str
    type: Literal["start_run"] = "start_run"


@dataclass(frozen=True, slots=True)
class CancelRun:
    run_id: str
    type: Literal["cancel_run"] = "cancel_run"


@dataclass(frozen=True, slots=True)
class DecideApproval:
    run_id: str
    call_id: str
    approved: bool
    type: Literal["decide_approval"] = "decide_approval"


@dataclass(frozen=True, slots=True)
class QueueRunInput:
    run_id: str
    text: str
    mode: Literal["steer", "follow_up"]
    type: Literal["queue_input"] = "queue_input"


@dataclass(frozen=True, slots=True)
class SetApprovalMode:
    mode: Literal["manual", "accept_edits", "plan", "auto"]
    confirmed: bool = False
    type: Literal["set_approval_mode"] = "set_approval_mode"


@dataclass(frozen=True, slots=True)
class SelectModel:
    model: str
    type: Literal["select_model"] = "select_model"


@dataclass(frozen=True, slots=True)
class RetryRun:
    session_id: str
    client_request_id: str
    type: Literal["retry_run"] = "retry_run"


@dataclass(frozen=True, slots=True)
class InvokeSkill:
    session_id: str
    name: str
    arguments: str
    client_request_id: str
    type: Literal["invoke_skill"] = "invoke_skill"


@dataclass(frozen=True, slots=True)
class InvokePrompt:
    session_id: str
    reference: str
    arguments: dict[str, str]
    display_input: str
    client_request_id: str
    type: Literal["invoke_prompt"] = "invoke_prompt"


@dataclass(frozen=True, slots=True)
class ListContextSources:
    session_id: str
    type: Literal["list_context_sources"] = "list_context_sources"


@dataclass(frozen=True, slots=True)
class ListMcpPrompts:
    type: Literal["list_mcp_prompts"] = "list_mcp_prompts"


@dataclass(frozen=True, slots=True)
class ListHooks:
    type: Literal["list_hooks"] = "list_hooks"


@dataclass(frozen=True, slots=True)
class SetContextSource:
    session_id: str
    kind: Literal["skill", "resource"]
    reference: str
    active: bool
    type: Literal["set_context_source"] = "set_context_source"


@dataclass(frozen=True, slots=True)
class CancelClarification:
    session_id: str
    type: Literal["cancel_clarification"] = "cancel_clarification"


@dataclass(frozen=True, slots=True)
class ContextControl:
    session_id: str
    control: Literal["report", "compact", "memory"]
    focus: str | None = None
    action: str | None = None
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    type: Literal["context_control"] = "context_control"


WorkspaceCommand: TypeAlias = (
    CreateSession
    | GetBootstrap
    | ListSessions
    | StartRun
    | CancelRun
    | DecideApproval
    | QueueRunInput
    | SetApprovalMode
    | SelectModel
    | RetryRun
    | InvokeSkill
    | InvokePrompt
    | ListContextSources
    | ListMcpPrompts
    | ListHooks
    | SetContextSource
    | CancelClarification
    | ContextControl
)


@dataclass(frozen=True, slots=True)
class SessionCreated:
    session_id: str


@dataclass(frozen=True, slots=True)
class RunStartedResult:
    run_id: str
    session_id: str
    status: str = "running"


@dataclass(frozen=True, slots=True)
class CommandAcknowledged:
    status: str
    data: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrap:
    agent: str
    workspace: str
    active_model: str
    model_id: str
    available_models: list[str]
    approval_mode: str
    tools: list[dict[str, str]]
    mcp: list[dict[str, object]]
    skills: list[dict[str, str]]
    warnings: list[str]
    active_run_id: str | None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    created_at: str
    model_id: str
    title: str


@dataclass(frozen=True, slots=True)
class SessionList:
    sessions: list[SessionSummary]


CommandResult: TypeAlias = (
    SessionCreated | RunStartedResult | CommandAcknowledged | WorkspaceBootstrap | SessionList
)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    sequence: int
    session_id: str
    run_id: str
    type: str
    data: dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    version: int = 1


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    model_id: str
    created_at: str
    plan: dict[str, Any]
    timeline: list[TimelineItem]
    last_user_input: str | None
    active_run_id: str | None
    pending_clarification: dict[str, Any] | None = None


class WorkspaceHostError(RuntimeError):
    code = "workspace_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class WorkspaceBusyError(WorkspaceHostError):
    code = "workspace_busy"


class SessionNotFoundError(WorkspaceHostError):
    code = "session_not_found"


class RunNotFoundError(WorkspaceHostError):
    code = "run_not_found"


class ApprovalStateError(WorkspaceHostError):
    code = "approval_not_pending"


class ApprovalAlreadyResolvedError(ApprovalStateError):
    code = "approval_already_resolved"


class InvalidStateError(WorkspaceHostError):
    code = "invalid_state"
