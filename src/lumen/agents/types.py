"""Durable types for Lumen's session-scoped multi-agent runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumen.plan import EvidenceReceipt
from lumen.work_products import EffectReceipt


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceMode(StrEnum):
    READ_ONLY = "read-only"
    WORKTREE = "worktree"


class AgentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    APPROVAL_PENDING = "approval_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    IMPORT_PENDING = "import_pending"
    IMPORTED = "imported"
    REJECTED = "rejected"
    CLOSED = "closed"
    NOT_CARRIED = "not_carried"


ACTIVE_AGENT_STATUSES = frozenset(
    {
        AgentStatus.QUEUED,
        AgentStatus.RUNNING,
        AgentStatus.WAITING,
        AgentStatus.APPROVAL_PENDING,
    }
)
class AgentEventKind(StrEnum):
    SPAWNED = "agent.spawned"
    STARTED = "agent.started"
    PROGRESS = "agent.progress"
    MESSAGE = "agent.message"
    APPROVAL_REQUESTED = "agent.approval_requested"
    COMPLETED = "agent.completed"
    FAILED = "agent.failed"
    INTERRUPTED = "agent.interrupted"
    RECONCILIATION_REQUIRED = "agent.reconciliation_required"
    IMPORT_PENDING = "agent.import_pending"
    IMPORTED = "agent.imported"
    REJECTED = "agent.rejected"
    CLOSED = "agent.closed"


class AgentProfile(BaseModel):
    """Auditable role configuration loaded before an agent is spawned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1)
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY
    tools: tuple[str, ...] | None = None
    model: str | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    source: str = "builtin"
    file_path: str | None = None
    revision: str = ""


class AgentToolPolicy(BaseModel):
    """Effective per-tool policy frozen into an Agent configuration snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    origin: str
    risk: str
    effect_kind: str


class AgentConfigSnapshot(BaseModel):
    """Effective, immutable child configuration captured at spawn time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_name: str
    model_id: str
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    tool_names: tuple[str, ...] = ()
    tool_policies: tuple[AgentToolPolicy, ...] = ()
    approval_mode: str = "manual"
    sandbox_mode: str = "disabled"
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY
    cwd: str
    request_limit: int | None = Field(default=None, ge=1)
    tool_call_limit: int | None = Field(default=None, ge=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    profile_revision: str = ""


class AgentThreadRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    path: str
    parent_session_id: str
    parent_agent_id: str = "/root"
    root_run_id: str
    agent_type: str


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    agent_id: str
    sender: str
    content: str | None = None
    artifact_ref: str | None = None
    trigger_turn: bool = False
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_content(self) -> AgentMessage:
        if not self.content and not self.artifact_ref:
            raise ValueError("agent message requires content or artifact_ref")
        return self


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    generation: int = Field(default=1, ge=1)
    status: AgentStatus
    summary: str = ""
    artifact_ref: str | None = None
    transcript_ref: str | None = None
    error: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict[str, Any])
    evidence: tuple[EvidenceReceipt, ...] = ()
    effect_receipts: tuple[EffectReceipt, ...] = ()
    delivered: bool = False
    created_at: str = Field(default_factory=utc_now)


class AgentThreadState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: AgentThreadRef
    task: str
    task_name: str
    status: AgentStatus = AgentStatus.QUEUED
    generation: int = Field(default=1, ge=1)
    config: AgentConfigSnapshot
    plan_step_id: str | None = None
    criterion_ids: tuple[str, ...] = ()
    idempotency_key: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    result: AgentResult | None = None
    resolution: str | None = None
    resolution_reason: str | None = None
    worktree: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    commit: str | None = None
    diff_artifact_ref: str | None = None
    history_ref: str | None = None
    parent_dirty_hash: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict[str, Any])


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    sequence: int = Field(ge=1)
    agent_id: str
    session_id: str
    root_run_id: str
    kind: AgentEventKind
    status: AgentStatus
    data: dict[str, Any] = Field(default_factory=dict[str, Any])
    created_at: str = Field(default_factory=utc_now)


class SessionAgentState(BaseModel):
    """Materialized projection of append-only v8 agent records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threads: tuple[AgentThreadState, ...] = ()
    events: tuple[AgentEvent, ...] = ()
    messages: tuple[AgentMessage, ...] = ()

    def get(self, agent_id: str) -> AgentThreadState | None:
        return next((item for item in self.threads if item.ref.id == agent_id), None)

    def upsert_thread(self, thread: AgentThreadState) -> SessionAgentState:
        items = [item for item in self.threads if item.ref.id != thread.ref.id]
        items.append(thread)
        items.sort(key=lambda item: (item.created_at, item.ref.id))
        return self.model_copy(update={"threads": tuple(items)})

    def append_event(self, event: AgentEvent) -> SessionAgentState:
        return self.model_copy(update={"events": (*self.events, event)})

    def append_message(self, message: AgentMessage) -> SessionAgentState:
        return self.model_copy(update={"messages": (*self.messages, message)})


class AgentExecutionResult(BaseModel):
    """Result returned by the internal child-runtime seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AgentStatus = AgentStatus.COMPLETED
    output: str = ""
    transcript: str = ""
    usage: dict[str, Any] = Field(default_factory=dict[str, Any])
    error: str | None = None
    worktree: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    commit: str | None = None
    diff: str = ""
    effect_receipts: tuple[EffectReceipt, ...] = ()


__all__ = [
    "ACTIVE_AGENT_STATUSES",
    "AgentConfigSnapshot",
    "AgentEvent",
    "AgentEventKind",
    "AgentExecutionResult",
    "AgentMessage",
    "AgentProfile",
    "AgentResult",
    "AgentStatus",
    "AgentThreadRef",
    "AgentThreadState",
    "AgentToolPolicy",
    "SessionAgentState",
    "WorkspaceMode",
]
