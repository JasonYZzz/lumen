"""Deprecated Child Run DTOs retained for compatibility responses.

Execution belongs exclusively to :mod:`lumen.agents.orchestrator`.  These
types keep the old Host/tool response shape without preserving a second Agent
lifecycle, worktree implementation, or persistence authority.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from lumen.plan import EvidenceReceipt


class ChildKind(StrEnum):
    RESEARCH = "research"
    WORKTREE = "worktree"


class ChildStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IMPORT_PENDING = "import_pending"
    IMPORTED = "imported"
    REJECTED = "rejected"
    CLOSED = "closed"


class ChildRunRecord(BaseModel):
    """Compatibility projection of one native Agent Thread."""

    model_config = ConfigDict(extra="forbid")

    id: str
    parent_session_id: str
    plan_step_id: str | None = None
    kind: ChildKind
    task: str
    status: ChildStatus = ChildStatus.QUEUED
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    result: str = ""
    error: str | None = None
    worktree: str | None = None
    branch: str | None = None
    commit: str | None = None
    base_commit: str | None = None
    target_head: str | None = None
    diff: str = ""
    evidence: list[EvidenceReceipt] = Field(default_factory=list[EvidenceReceipt])


__all__ = ["ChildKind", "ChildRunRecord", "ChildStatus"]
