from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeAlias

from lumen.attachments import AttachmentRef
from lumen.reasoning import ReasoningLevel, ReasoningSelection
from lumen.timeline import TimelineItem


@dataclass(frozen=True, slots=True)
class CreateSession:
    type: Literal["create_session"] = "create_session"


@dataclass(frozen=True, slots=True)
class GetBootstrap:
    type: Literal["get_bootstrap"] = "get_bootstrap"


@dataclass(frozen=True, slots=True)
class GetConfiguration:
    type: Literal["get_configuration"] = "get_configuration"


@dataclass(frozen=True, slots=True)
class GetInstructions:
    type: Literal["get_instructions"] = "get_instructions"


@dataclass(frozen=True, slots=True)
class InspectReasoning:
    definition: dict[str, Any]
    type: Literal["inspect_reasoning"] = "inspect_reasoning"


@dataclass(frozen=True, slots=True)
class UpsertModelConfiguration:
    expected_revision: str
    name: str
    definition: dict[str, Any]
    set_default: bool = False
    type: Literal["upsert_model_configuration"] = "upsert_model_configuration"


@dataclass(frozen=True, slots=True)
class SetMcpServerEnabled:
    expected_revision: str
    name: str
    enabled: bool
    type: Literal["set_mcp_server_enabled"] = "set_mcp_server_enabled"


@dataclass(frozen=True, slots=True)
class DeleteModelConfiguration:
    expected_revision: str
    name: str
    type: Literal["delete_model_configuration"] = "delete_model_configuration"


@dataclass(frozen=True, slots=True)
class ListSessions:
    include_archived: bool = False
    type: Literal["list_sessions"] = "list_sessions"


@dataclass(frozen=True, slots=True)
class RenameSession:
    session_id: str
    title: str
    type: Literal["rename_session"] = "rename_session"


@dataclass(frozen=True, slots=True)
class SetSessionArchived:
    session_id: str
    archived: bool
    type: Literal["set_session_archived"] = "set_session_archived"


@dataclass(frozen=True, slots=True)
class DeleteSession:
    session_id: str
    type: Literal["delete_session"] = "delete_session"


@dataclass(frozen=True, slots=True)
class StartRun:
    session_id: str
    input: str
    client_request_id: str
    model_prompt: str | None = None
    attachments: tuple[AttachmentRef | dict[str, Any], ...] = ()
    regenerate_from_turn: int | None = None
    type: Literal["start_run"] = "start_run"


@dataclass(frozen=True, slots=True)
class RunDirectCommand:
    session_id: str
    argv: tuple[str, ...]
    client_request_id: str
    type: Literal["run_direct_command"] = "run_direct_command"


@dataclass(frozen=True, slots=True)
class CancelRun:
    run_id: str
    type: Literal["cancel_run"] = "cancel_run"


@dataclass(frozen=True, slots=True)
class StartLiveSession:
    session_id: str
    client_request_id: str
    sdp: str | None = None
    route: str | None = None
    type: Literal["start_live_session"] = "start_live_session"


@dataclass(frozen=True, slots=True)
class InterruptLiveSession:
    live_session_id: str
    type: Literal["interrupt_live_session"] = "interrupt_live_session"


@dataclass(frozen=True, slots=True)
class EndLiveSession:
    live_session_id: str
    type: Literal["end_live_session"] = "end_live_session"


@dataclass(frozen=True, slots=True)
class DecideApproval:
    run_id: str
    call_id: str
    approved: bool
    scope: Literal["once", "session", "always"] = "once"
    type: Literal["decide_approval"] = "decide_approval"


@dataclass(frozen=True, slots=True)
class QueueRunInput:
    run_id: str
    text: str
    mode: Literal["steer", "follow_up"]
    model_prompt: str | None = None
    attachments: tuple[AttachmentRef | dict[str, Any], ...] = ()
    type: Literal["queue_input"] = "queue_input"


@dataclass(frozen=True, slots=True)
class DequeueRunInputs:
    run_id: str
    type: Literal["dequeue_run_inputs"] = "dequeue_run_inputs"


@dataclass(frozen=True, slots=True)
class SetApprovalMode:
    session_id: str
    mode: Literal["manual", "accept_edits", "auto"]
    type: Literal["set_approval_mode"] = "set_approval_mode"


@dataclass(frozen=True, slots=True)
class SetCollaborationMode:
    session_id: str
    mode: Literal["default", "plan"]
    type: Literal["set_collaboration_mode"] = "set_collaboration_mode"


@dataclass(frozen=True, slots=True)
class SetTranscriptDensity:
    session_id: str
    density: Literal["normal", "verbose"]
    type: Literal["set_transcript_density"] = "set_transcript_density"


@dataclass(frozen=True, slots=True)
class ApprovePlan:
    session_id: str
    revision: int
    client_request_id: str
    type: Literal["approve_plan"] = "approve_plan"


@dataclass(frozen=True, slots=True)
class RejectPlan:
    session_id: str
    revision: int
    feedback: str
    client_request_id: str
    type: Literal["reject_plan"] = "reject_plan"


@dataclass(frozen=True, slots=True)
class WaivePlanVerification:
    session_id: str
    scope: tuple[str, ...]
    reason: str
    type: Literal["waive_plan_verification"] = "waive_plan_verification"


@dataclass(frozen=True, slots=True)
class ListChildRuns:
    session_id: str
    type: Literal["list_child_runs"] = "list_child_runs"


@dataclass(frozen=True, slots=True)
class ListAgents:
    session_id: str
    type: Literal["list_agents"] = "list_agents"


@dataclass(frozen=True, slots=True)
class SendAgentMessage:
    agent_id: str
    message: str
    type: Literal["send_agent_message"] = "send_agent_message"


@dataclass(frozen=True, slots=True)
class ContinueAgent:
    agent_id: str
    task: str
    type: Literal["continue_agent"] = "continue_agent"


@dataclass(frozen=True, slots=True)
class InterruptAgent:
    agent_id: str
    type: Literal["interrupt_agent"] = "interrupt_agent"


@dataclass(frozen=True, slots=True)
class CloseAgent:
    agent_id: str
    resolution: str | None = None
    reason: str | None = None
    type: Literal["close_agent"] = "close_agent"
@dataclass(frozen=True, slots=True)
class ApproveAgentImport:
    agent_id: str
    type: Literal["approve_agent_import"] = "approve_agent_import"


@dataclass(frozen=True, slots=True)
class RejectAgentImport:
    agent_id: str
    type: Literal["reject_agent_import"] = "reject_agent_import"


@dataclass(frozen=True, slots=True)
class ListCheckpoints:
    session_id: str
    type: Literal["list_checkpoints"] = "list_checkpoints"


@dataclass(frozen=True, slots=True)
class ForkSessionAtTurn:
    session_id: str
    through_turn: int
    include_turn: bool = True
    client_request_id: str | None = None
    type: Literal["fork_session_at_turn"] = "fork_session_at_turn"


@dataclass(frozen=True, slots=True)
class CancelChildRun:
    child_id: str
    type: Literal["cancel_child_run"] = "cancel_child_run"


@dataclass(frozen=True, slots=True)
class ApproveChildImport:
    child_id: str
    type: Literal["approve_child_import"] = "approve_child_import"


@dataclass(frozen=True, slots=True)
class RejectChildImport:
    child_id: str
    type: Literal["reject_child_import"] = "reject_child_import"


@dataclass(frozen=True, slots=True)
class CloseChildRun:
    child_id: str
    type: Literal["close_child_run"] = "close_child_run"


@dataclass(frozen=True, slots=True)
class SelectModel:
    model: str
    type: Literal["select_model"] = "select_model"


@dataclass(frozen=True, slots=True)
class SelectReasoning:
    session_id: str
    effort: ReasoningLevel
    type: Literal["select_reasoning"] = "select_reasoning"


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
    regenerate_from_turn: int | None = None
    type: Literal["invoke_skill"] = "invoke_skill"


@dataclass(frozen=True, slots=True)
class InvokePrompt:
    session_id: str
    reference: str
    arguments: dict[str, str]
    display_input: str
    client_request_id: str
    regenerate_from_turn: int | None = None
    type: Literal["invoke_prompt"] = "invoke_prompt"


@dataclass(frozen=True, slots=True)
class ListContextSources:
    session_id: str
    type: Literal["list_context_sources"] = "list_context_sources"


@dataclass(frozen=True, slots=True)
class ListMcpPrompts:
    type: Literal["list_mcp_prompts"] = "list_mcp_prompts"


@dataclass(frozen=True, slots=True)
class ListMcpResources:
    session_id: str | None = None
    type: Literal["list_mcp_resources"] = "list_mcp_resources"


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


@dataclass(frozen=True, slots=True)
class StoreAttachment:
    filename: str
    media_type: str
    content: bytes
    type: Literal["store_attachment"] = "store_attachment"


@dataclass(frozen=True, slots=True)
class ImportAttachmentPath:
    path: str
    type: Literal["import_attachment_path"] = "import_attachment_path"


WorkspaceCommand: TypeAlias = (
    CreateSession
    | GetBootstrap
    | GetConfiguration
    | GetInstructions
    | InspectReasoning
    | UpsertModelConfiguration
    | SetMcpServerEnabled
    | DeleteModelConfiguration
    | ListSessions
    | RenameSession
    | SetSessionArchived
    | DeleteSession
    | StartRun
    | RunDirectCommand
    | CancelRun
    | StartLiveSession
    | InterruptLiveSession
    | EndLiveSession
    | DecideApproval
    | QueueRunInput
    | DequeueRunInputs
    | SetApprovalMode
    | SetCollaborationMode
    | SetTranscriptDensity
    | ApprovePlan
    | RejectPlan
    | WaivePlanVerification
    | ListChildRuns
    | ListAgents
    | SendAgentMessage
    | ContinueAgent
    | InterruptAgent
    | CloseAgent
    | ApproveAgentImport
    | RejectAgentImport
    | ListCheckpoints
    | ForkSessionAtTurn
    | CancelChildRun
    | ApproveChildImport
    | RejectChildImport
    | CloseChildRun
    | SelectModel
    | SelectReasoning
    | RetryRun
    | InvokeSkill
    | InvokePrompt
    | ListContextSources
    | ListMcpPrompts
    | ListMcpResources
    | ListHooks
    | SetContextSource
    | CancelClarification
    | ContextControl
    | StoreAttachment
    | ImportAttachmentPath
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
class LiveStartedResult:
    live_session_id: str
    session_id: str
    media: dict[str, Any]
    answer_sdp: str | None
    state: dict[str, Any]


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
    input_modalities: list[str]
    available_models: list[str]
    approval_mode: str
    collaboration_mode: str
    tools: list[dict[str, str]]
    mcp: list[dict[str, object]]
    skills: list[dict[str, str]]
    warnings: list[str]
    active_run_id: str | None
    live_enabled: bool = False
    reasoning: ReasoningSelection = field(default_factory=ReasoningSelection)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    created_at: str
    model_id: str
    title: str
    archived: bool = False
    title_pending: bool = False


@dataclass(frozen=True, slots=True)
class SessionList:
    sessions: list[SessionSummary]


CommandResult: TypeAlias = (
    SessionCreated
    | RunStartedResult
    | LiveStartedResult
    | CommandAcknowledged
    | WorkspaceBootstrap
    | SessionList
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
    approval_mode: str = "manual"
    collaboration_mode: str = "default"
    plan_review_status: str = "none"
    transcript_density: str = "normal"
    pending_clarification: dict[str, Any] | None = None
    work_products: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    pending_effects: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    recoverable_effects: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    agents: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    agent_usage: dict[str, Any] = field(default_factory=dict[str, Any])
    live_sessions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    reasoning: ReasoningSelection = field(default_factory=ReasoningSelection)


class WorkspaceHostError(RuntimeError):
    code = "workspace_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class WorkspaceBusyError(WorkspaceHostError):
    code = "workspace_busy"


class ConfigurationConflictHostError(WorkspaceHostError):
    code = "configuration_conflict"


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
