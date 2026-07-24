"""Data contracts for the structured context engine (M0).

These are the frozen, round-trip-stable domain types the context engine v2
will be built on:

* :class:`ContextZone` / :class:`ContextBlock` - the structured partition model
  (plan §6). Every model-visible byte lives in a labelled, source-tracked block.
* :class:`ModelContextSpec` / :class:`ContextBudgetReport` - provider-aware
  window sizing and the per-zone budget the ``/context`` command renders
  (plan §8, §14.1).
* :class:`ToolReceipt` - the content-addressed receipt that replaces bulky tool
  outputs in active history (plan §9.3).
* :class:`CompactionCheckpointV1` - the versioned, source-ranged, exact-literal
  checkpoint that supersedes the free-text ``ContextSummary`` (plan §10).

This module is deliberately contract-only: it defines the types and their
JSON-schema validation, but wires nothing into the running engine yet. M1
folds these into the ``lumen.context`` package as ``types``; until then they
are produced and consumed only by tests so the contract is provable before any
behaviour depends on it.

Design choice: the plan sketches these as ``@dataclass(frozen=True, slots=True)``
but the M0 acceptance criterion is "all schemas stably round-trip" and §10.2
mandates Pydantic/schema validation. Pydantic ``BaseModel(frozen=True)`` is also
the established project pattern for serialisable domain models (``ContextSummary``,
``PlanState``), so it is used here. ``slots=True`` is omitted because Pydantic v2
``BaseModel`` does not support it; immutability is preserved via ``frozen=True``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Contract(BaseModel):
    """Shared config: frozen, no extra fields, JSON-round-trippable."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Zones, retention, trust (plan §6.1, §6.2, §19)
# --------------------------------------------------------------------------- #


class ContextZone(StrEnum):
    """Every byte of model-visible context lives in exactly one labelled zone.

    The ordering mirrors the assembly order in plan §8.2: stable/pinned content
    first, then session state, then the compressible recent tail, then the
    per-turn ephemera. ``OUTPUT_RESERVE`` is measured but never rendered.
    """

    SYSTEM = "system"
    POLICY = "policy"
    MEMORY_INDEX = "memory_index"
    CAPABILITY_CATALOG = "capability_catalog"
    TASK_STATE = "task_state"
    HISTORY_SUMMARY = "history_summary"
    RECENT_HISTORY = "recent_history"
    RECALLED_MEMORY = "recalled_memory"
    RETRIEVED_CONTEXT = "retrieved_context"
    CURRENT_INPUT = "current_input"
    OUTPUT_RESERVE = "output_reserve"


class RetentionPolicy(StrEnum):
    """How a block survives a compaction cycle (plan §6.1 "压缩行为" column)."""

    PINNED = "pinned"
    REINJECT = "reinject"
    SUMMARIZE = "summarize"
    REDUCE = "reduce"
    EPHEMERAL = "ephemeral"
    RESERVE_ONLY = "reserve_only"


class TrustLevel(StrEnum):
    """Provenance trust tag. External text is always ``UNTRUSTED_EXTERNAL`` and
    may never be re-tagged as system/policy/trusted memory (plan §19)."""

    SYSTEM = "system"
    POLICY = "policy"
    DURABLE = "durable"
    RECALLED = "recalled"
    UNTRUSTED_EXTERNAL = "untrusted_external"


class SourceKind(StrEnum):
    """The category of origin a block or checkpoint item was derived from."""

    SYSTEM = "system"
    POLICY = "policy"
    MEMORY = "memory"
    CAPABILITY = "capability"
    TASK = "task"
    HISTORY = "history"
    RETRIEVED = "retrieved"
    CURRENT = "current"
    RESERVE = "reserve"


# --------------------------------------------------------------------------- #
# Block provenance and payload
# --------------------------------------------------------------------------- #


class EvidenceRef(BaseModel):
    """Pointer back to the transcript event/message a block or item came from.

    At least one of ``event_id`` / ``message_index`` / ``tool_call_id`` is
    expected in practice; all are optional so a synthesised block (e.g. a
    re-injected system prompt) can carry only a ``detail`` note.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str | None = None
    event_id: str | None = None
    message_index: int | None = None
    tool_call_id: str | None = None
    detail: str | None = None


class ContextSource(BaseModel):
    """Authoritative origin of a block, for provenance and revision dedup.

    ``revision`` is a content digest or version stamp: two blocks with the same
    ``source`` revision must not both be injected (plan §6.2 invariant).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SourceKind
    origin: str
    revision: str | None = None


class ContextPayload(BaseModel):
    """The renderable content of a block.

    ``text`` is the model-visible string; ``structured`` holds a typed
    alternative (tool-schema list, memory entries, checkpoint items) so a
    block can be rendered or inspected without re-parsing prose.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = None
    structured: dict[str, Any] | None = None


class ContextBlock(_Contract):
    """One unit of model-visible context (plan §6.2).

    Invariants enforced structurally: a non-negative token estimate, a priority
    in the plan's 0-100 band, and an explicit retention policy. External content
    callers must pair ``trust=UNTRUSTED_EXTERNAL`` themselves; this type does not
    infer it.
    """

    id: str
    zone: ContextZone
    source: ContextSource
    payload: ContextPayload
    token_estimate: int = Field(ge=0)
    priority: int = Field(ge=0, le=100)
    retention: RetentionPolicy
    trust: TrustLevel
    cache_key: str | None = None
    provenance: tuple[EvidenceRef, ...] = Field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Provider-aware window sizing (plan §8.1, §8.3, §14.1)
# --------------------------------------------------------------------------- #


class ModelContextSpec(_Contract):
    """Resolved per-model context capabilities (plan §8.1).

    Resolution order (handled in M2, not here): explicit model config >
    provider/model profile > known-model table > conservative default. This type
    is the *result* of that resolution; ``tokenizer`` identifies which
    :class:`TokenCounter` adapter applies.
    """

    context_window_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    tokenizer: str
    supports_remote_compaction: bool = False
    supports_tool_search: bool = False


class ZoneUsage(_Contract):
    """One row of the ``/context`` zone table (plan §14.1)."""

    zone: ContextZone
    tokens: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)
    survival: RetentionPolicy


class PressureItem(_Contract):
    """A named top-cost item surfaced by ``/context`` (e.g. an MCP schema)."""

    label: str
    tokens: int = Field(ge=0)
    source: str | None = None


class ContextBudgetReport(_Contract):
    """The full budget picture rendered by ``/context`` (plan §8.3, §14.1).

    ``estimated`` marks that the model window came from a conservative fallback
    rather than a known profile, so ``/context`` can flag it.
    """

    context_window_tokens: int = Field(gt=0)
    used_tokens: int = Field(ge=0)
    output_reserve_tokens: int = Field(ge=0)
    soft_threshold_tokens: int = Field(gt=0)
    hard_threshold_tokens: int = Field(gt=0)
    target_tokens: int = Field(gt=0)
    estimated: bool = False
    zones: tuple[ZoneUsage, ...] = Field(default_factory=tuple)
    pressure: tuple[PressureItem, ...] = Field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Tool receipts (plan §9.3)
# --------------------------------------------------------------------------- #


class ToolReceipt(_Contract):
    """Content-addressed receipt that replaces a bulky tool output in history.

    The full body lives in an artifact store keyed by ``sha256``; ``head``/``tail``
    keep enough context for the model to reason without re-loading the artifact.
    ``artifact_ref`` is ``None`` when the output was small enough to inline (or
    when the tool declared ``artifact_policy=never`` for secret-bearing output).
    """

    tool_call_id: str
    tool_name: str
    status: Literal["success", "error", "cancelled"]
    summary: str
    head: str = ""
    tail: str = ""
    byte_size: int = Field(ge=0)
    sha256: str
    artifact_ref: str | None = None
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Structured checkpoint (plan §10.1)
# --------------------------------------------------------------------------- #


class ObservationState(StrEnum):
    """Distinguishes user-claimed, tool-observed and model-inferred facts.

    Only ``observed`` may be marked as a verified result; ``claimed`` is what the
    user said, ``inferred`` is what the model guessed (plan §10.1).
    """

    CLAIMED = "claimed"
    OBSERVED = "observed"
    INFERRED = "inferred"


class CheckpointItem(_Contract):
    """One structured entry in a checkpoint section (plan §10.1).

    Every item carries a stable ``id``, its source events, a confidence and the
    ids of earlier items it supersedes - so consolidation never silently
    overwrites a contradictory fact.
    """

    id: str
    text: str
    status: str | None = None
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    supersedes: tuple[str, ...] = Field(default_factory=tuple)


class FileState(_Contract):
    """A file's state, tagged by how it was learned (plan §10.1)."""

    path: str
    state: ObservationState
    detail: str | None = None
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)


class ExecutionState(_Contract):
    """A command/test execution, tagged by how it was learned (plan §10.1)."""

    command: str
    exit_code: int | None = None
    state: ObservationState
    detail: str | None = None
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)


class ExactLiteral(_Contract):
    """A value that must survive compaction verbatim (plan §10.2).

    File paths, symbols, commands, URLs, error codes, version numbers and
    user-requested literal text go here so summarisation cannot paraphrase them
    away.
    """

    kind: Literal["path", "symbol", "command", "url", "error_code", "version", "user_text"]
    value: str
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)


class MemoryCandidateRef(_Contract):
    """A fact worth remembering, pending the two-stage memory flow (plan §11.4).

    A checkpoint only *references* candidates; consolidation into durable memory
    is a separate, opt-in stage, so a checkpoint never silently becomes long-term
    knowledge.
    """

    content: str
    scope: Literal["user", "project", "path", "agent"] = "project"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)


class CompactionCheckpointV1(_Contract):
    """Versioned, source-ranged compaction checkpoint (plan §10.1).

    Replaces the eight free-text lists of :class:`ContextSummary` with a
    structured, source-tracked, exact-literal-preserving snapshot. The
    ``source_start``/``source_end`` range is a *continuous, non-overlapping*
    slice of the transcript event sequence; a new checkpoint references its
    ``parent_checkpoint_id`` so the delta to summarise is always well-defined
    (plan §9.2 step 5, §10.2).
    """

    schema_version: Literal[1] = 1
    checkpoint_id: str
    parent_checkpoint_id: str | None = None
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    source_digest: str
    created_at: datetime
    focus: str | None = None
    objective: str = ""
    constraints: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    decisions: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    plan: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    completed: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    files: tuple[FileState, ...] = Field(default_factory=tuple)
    commands_and_tests: tuple[ExecutionState, ...] = Field(default_factory=tuple)
    failures: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    approvals: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    open_questions: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    next_actions: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    exact_literals: tuple[ExactLiteral, ...] = Field(default_factory=tuple)
    uncertainties: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    do_not_repeat: tuple[CheckpointItem, ...] = Field(default_factory=tuple)
    memory_candidates: tuple[MemoryCandidateRef, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_source_range(self) -> CompactionCheckpointV1:
        """A checkpoint covers a continuous, non-negative event range."""

        if self.source_end < self.source_start:
            raise ValueError(
                f"checkpoint source_end ({self.source_end}) must be >= source_start ({self.source_start})"
            )
        return self


__all__ = [
    "CheckpointItem",
    "CompactionCheckpointV1",
    "ContextBlock",
    "ContextBudgetReport",
    "ContextPayload",
    "ContextSource",
    "ContextZone",
    "EvidenceRef",
    "ExactLiteral",
    "ExecutionState",
    "FileState",
    "MemoryCandidateRef",
    "ModelContextSpec",
    "ObservationState",
    "PressureItem",
    "RetentionPolicy",
    "SourceKind",
    "ToolReceipt",
    "TrustLevel",
    "ZoneUsage",
]
