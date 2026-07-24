"""Lumen context engine package.

Public Seam: :class:`~lumen.context.engine.ContextEngine` with
``prepare`` / ``commit`` / ``control`` and the high-level DTOs in
:mod:`lumen.context.engine`. Callers (the runtime, the TUI) depend only on
those, never on the summary prompt, the token estimator or the compaction
internals.

Layout (plan §16):

* :mod:`lumen.context.types`     - frozen, round-trip-stable domain contracts
  (zones, blocks, budget report, receipts, checkpoint). Pure, no lumen deps.
* :mod:`lumen.context.legacy`   - the proven ``ContextManager`` compaction
  implementation, wrapped by the engine. Retained for the deprecation period.
* :mod:`lumen.context.engine`   - the ``ContextEngine`` Seam and its DTOs.

This ``__init__`` re-exports the legacy names so existing
``from lumen.context import ContextManager, ContextSummary, ...`` imports keep
working unchanged during the migration. The legacy names emit no warning yet;
M8 removes them once no caller remains.
"""

from __future__ import annotations

from lumen.context.artifacts import ArtifactStore, ArtifactStoreError
from lumen.context.assembler import AssembledContext, ContextAssembler, ZoneCaps
from lumen.context.budget import (
    ConservativeTokenCounter,
    DeterministicTokenCounter,
    ProviderTokenCounter,
    TokenCount,
    TokenCounter,
    resolve_model_spec,
    select_token_counter,
)
from lumen.context.compaction import (
    CompactionPolicy,
    CompactionThrashState,
    FixedContextTooLarge,
    Thresholds,
    degrade_to_window,
)
from lumen.context.engine import (
    AgentRef,
    ContextCommit,
    ContextCompactCommand,
    ContextControlResult,
    ContextEngine,
    ContextEnvelope,
    ContextMemoryCommand,
    ContextReportCommand,
    ContextRequest,
    ContextSequenceError,
    ContextTransition,
    EventSink,
    PreviousSummary,
    RuntimeContextSnapshot,
    SessionRef,
    TaskSnapshot,
)
from lumen.context.legacy import (
    CompactionRecord,
    ContextBudgetExceeded,
    ContextManager,
    ContextReservation,
    ContextSummary,
    PreparedContext,
    RequestBudgetEstimator,
    estimate_message_tokens,
    retain_recent_tokens,
    retain_recent_turns,
    validate_active_history,
)
from lumen.context.transcript import ReductionResult, build_receipt, reduce_tool_outputs
from lumen.context.types import (
    CheckpointItem,
    CompactionCheckpointV1,
    ContextBlock,
    ContextBudgetReport,
    ContextPayload,
    ContextSource,
    ContextZone,
    EvidenceRef,
    ExactLiteral,
    ExecutionState,
    FileState,
    MemoryCandidateRef,
    ModelContextSpec,
    ObservationState,
    PressureItem,
    RetentionPolicy,
    SourceKind,
    ToolReceipt,
    TrustLevel,
    ZoneUsage,
)

__all__ = [
    # engine Seam + DTOs
    "AgentRef",
    "ArtifactStore",
    "ArtifactStoreError",
    "AssembledContext",
    # domain contracts
    "CheckpointItem",
    "CompactionCheckpointV1",
    "CompactionPolicy",
    # legacy compat (deprecation period)
    "CompactionRecord",
    "CompactionThrashState",
    "ConservativeTokenCounter",
    "ContextAssembler",
    "ContextBlock",
    "ContextBudgetExceeded",
    "ContextBudgetReport",
    "ContextCommit",
    "ContextCompactCommand",
    "ContextControlResult",
    "ContextEngine",
    "ContextEnvelope",
    "ContextManager",
    "ContextMemoryCommand",
    "ContextPayload",
    "ContextReportCommand",
    "ContextRequest",
    "ContextReservation",
    "ContextSequenceError",
    "ContextSource",
    "ContextSummary",
    "ContextTransition",
    "ContextZone",
    "DeterministicTokenCounter",
    "EventSink",
    "EvidenceRef",
    "ExactLiteral",
    "ExecutionState",
    "FileState",
    "FixedContextTooLarge",
    "MemoryCandidateRef",
    "ModelContextSpec",
    "ObservationState",
    "PreparedContext",
    "PressureItem",
    "PreviousSummary",
    "ProviderTokenCounter",
    "ReductionResult",
    "RequestBudgetEstimator",
    "RetentionPolicy",
    "RuntimeContextSnapshot",
    "SessionRef",
    "SourceKind",
    "TaskSnapshot",
    "Thresholds",
    "TokenCount",
    "TokenCounter",
    "ToolReceipt",
    "TrustLevel",
    "ZoneCaps",
    "ZoneUsage",
    "build_receipt",
    "degrade_to_window",
    "estimate_message_tokens",
    "reduce_tool_outputs",
    "resolve_model_spec",
    "retain_recent_tokens",
    "retain_recent_turns",
    "select_token_counter",
    "validate_active_history",
]
