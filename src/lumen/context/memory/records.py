"""Memory domain types (M5).

Durable memory records are low-trust, reusable facts (a preferred command, an
architecture choice) - never permissions or commands (plan §11.1). They are
always "possibly helpful, verify before relying on" context; on conflict the
order is: current user instruction > project/user instructions > current repo
fact > durable memory.

MemoryRecord (plan §11.3) is the authoritative row; the repository stores it
and the projector renders a human-auditable MEMORY.md view from it. ``status``
tracks the lifecycle (candidate -> active -> conflicted/forgotten/expired) and
``supersedes`` links a record to the earlier ones it replaces, so consolidation
never silently overwrites a contradictory fact.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryScope(StrEnum):
    """Where a memory applies (plan §11.2)."""

    USER = "user"  # cross-project preference
    PROJECT = "project"  # shared by canonical repo identity, not worktree path
    PATH = "path"  # only recalled when accessing matching files
    AGENT = "agent"  # future multi-agent; first version is not auto-learned


class MemoryKind(StrEnum):
    """What flavour of fact a memory is (plan §11.3)."""

    PREFERENCE = "preference"
    WORKFLOW = "workflow"
    PROJECT_FACT = "project_fact"
    WARNING = "warning"
    REFERENCE = "reference"


class MemorySource(StrEnum):
    """How the fact was learned (plan §11.3)."""

    EXPLICIT = "explicit"  # user typed /memory remember
    OBSERVED = "observed"  # a tool verified it
    INFERRED = "inferred"  # the model guessed


class MemoryStatus(StrEnum):
    """Lifecycle state of a memory (plan §11.3)."""

    CANDIDATE = "candidate"  # awaiting consolidation
    ACTIVE = "active"  # recalled by default
    CONFLICTED = "conflicted"  # contradicted by another; not in default index
    FORGOTTEN = "forgotten"  # tombstoned by /memory forget; never resurrected
    EXPIRED = "expired"  # past valid_until


class Sensitivity(StrEnum):
    """How sensitive a memory's content is (plan §11.6)."""

    PUBLIC = "public"  # safe to project and log
    INTERNAL = "internal"  # project-internal
    RESTRICTED = "restricted"  # never written to Markdown projection or logs


class MemoryRecord(BaseModel):
    """One durable memory row (plan §11.3).

    Frozen and round-trip-stable like the other context contracts. Invariants:
    confidence is a 0-1 fraction, ``valid_until`` (when set) is after
    ``valid_from``, and a forgotten/expired record never reverts to active
    without a new explicit write.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    scope: MemoryScope
    kind: MemoryKind
    content: str
    source_session_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_kind: MemorySource = MemorySource.EXPLICIT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    use_count: int = Field(default=0, ge=0)
    valid_from: datetime
    valid_until: datetime | None = None
    supersedes: tuple[str, ...] = Field(default_factory=tuple)
    status: MemoryStatus = MemoryStatus.ACTIVE
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    #: Canonical repository identity. USER memories are intentionally global;
    #: project/path/agent memories are filtered by this key so worktrees share
    #: knowledge without leaking it into unrelated repositories.
    project_id: str | None = None
    #: Optional path glob for PATH-scope recall (plan §11.2).
    path_glob: str | None = None

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> MemoryRecord:
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from")
        if self.scope is MemoryScope.PATH and self.path_glob is None:
            raise ValueError("PATH-scope memory requires a path_glob")
        return self


def memory_record_id(scope: MemoryScope, content: str, *, project_id: str | None) -> str:
    """Return the stable authority key shared by explicit and learned memory."""

    namespace = "global" if scope is MemoryScope.USER else (project_id or "default")
    digest = hashlib.sha256(f"{namespace}:{scope.value}:{content}".encode()).hexdigest()
    return f"mem-{digest[:16]}"


__all__ = [
    "MemoryKind",
    "MemoryRecord",
    "MemoryScope",
    "MemorySource",
    "MemoryStatus",
    "Sensitivity",
    "memory_record_id",
]
