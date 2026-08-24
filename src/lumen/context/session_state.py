"""Session-scoped context source contracts.

The append-only session repository stores only metadata and content-addressed
artifact references. Exact Skill and MCP bodies live in the 0600 artifact
store, making resume reproducible without silently re-reading changed local or
remote sources.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _StateModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ActiveSkillRef(_StateModel):
    name: str
    revision: str
    source: str
    body_artifact_ref: str
    activated_at: datetime
    last_used_at: datetime


class ActiveResourceRef(_StateModel):
    reference: str
    server: str
    uri: str
    revision: str
    body_artifact_ref: str
    fetched_at: datetime
    ttl_seconds: int | None = Field(default=None, ge=0)
    refresh_on_resume: bool = False


class PendingClarification(_StateModel):
    id: str
    question: str = Field(min_length=1, max_length=2_000)
    choices: tuple[str, ...] = Field(default_factory=tuple, max_length=5)
    related_plan_step: str | None = None
    created_at: datetime


class SessionContextState(_StateModel):
    active_skills: tuple[ActiveSkillRef, ...] = Field(default_factory=tuple)
    active_resources: tuple[ActiveResourceRef, ...] = Field(default_factory=tuple)
    pending_clarification: PendingClarification | None = None


class ResolvedSessionContext(_StateModel):
    skill_documents: tuple[dict[str, object], ...] = Field(default_factory=tuple)
    resource_documents: tuple[dict[str, object], ...] = Field(default_factory=tuple)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)


__all__ = [
    "ActiveResourceRef",
    "ActiveSkillRef",
    "PendingClarification",
    "ResolvedSessionContext",
    "SessionContextState",
]
