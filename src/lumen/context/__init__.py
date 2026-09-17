"""Lumen context engine package.

Public Seam: :class:`~lumen.context.engine.ContextEngine` with
``prepare`` / ``commit`` / ``control`` and the high-level DTOs in
:mod:`lumen.context.engine`. Callers (the runtime, the TUI) depend only on
those, never on the summary prompt, the token estimator or the compaction
internals.

Layout:

* :mod:`lumen.context.types`     - frozen, round-trip-stable domain contracts
  (zones, blocks, budget report, receipts, checkpoint). Pure, no lumen deps.
* :mod:`lumen.context.legacy`   - the proven ``ContextManager`` compaction
  implementation, wrapped by the engine. Retained for the deprecation period.
* :mod:`lumen.context.engine`   - the ``ContextEngine`` Seam and its DTOs.

This ``__init__`` re-exports the legacy names so existing
``from lumen.context import ContextManager, ContextSummary, ...`` imports keep
working unchanged. Direct ``ContextManager`` construction emits a deprecation
warning; the engine still uses it internally for structured summarization.
Remove these exports only after public compatibility callers have migrated.
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
    TokenCounterFactory,
    TokenCounterSelection,
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
    ContextStateChange,
    ContextSummary,
    PreparedContext,
    RequestBudgetEstimator,
    SummaryResult,
    SummaryTaskState,
    estimate_message_tokens,
    merge_context_summary,
    retain_recent_tokens,
    retain_recent_turns,
    validate_active_history,
)
from lumen.context.profiles import (
    DEFAULT_UNKNOWN_OUTPUT_TOKENS,
    ModelCapabilityProfile,
    ResolvedContextPolicy,
    TokenizerSpec,
    profiles,
    resolve_context_policy,
)
from lumen.context.session_state import (
    ActiveResourceRef,
    ActiveSkillRef,
    PendingClarification,
    ResolvedSessionContext,
    SessionContextState,
)
from lumen.context.transcript import ReductionResult, build_receipt, reduce_tool_outputs
from lumen.context.types import (
    CheckpointItem,
    CompactionCheckpointV1,
    CompactionCheckpointV2,
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
    ModelInputManifest,
    ModelInputSource,
    ObservationState,
    PressureItem,
    ProviderRequestReceipt,
    ProviderRequestSnapshot,
    ReplayEligibility,
    RetentionPolicy,
    RollingContextState,
    SourceKind,
    ToolReceipt,
    TranscriptCursor,
    TrustLevel,
    ZoneUsage,
)

__all__ = [
    "DEFAULT_UNKNOWN_OUTPUT_TOKENS",
    # engine Seam + DTOs
    "ActiveResourceRef",
    "ActiveSkillRef",
    "AgentRef",
    "ArtifactStore",
    "ArtifactStoreError",
    "AssembledContext",
    # domain contracts
    "CheckpointItem",
    "CompactionCheckpointV1",
    "CompactionCheckpointV2",
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
    "ContextStateChange",
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
    "ModelCapabilityProfile",
    "ModelContextSpec",
    "ModelInputManifest",
    "ModelInputSource",
    "ObservationState",
    "PendingClarification",
    "PreparedContext",
    "PressureItem",
    "PreviousSummary",
    "ProviderRequestReceipt",
    "ProviderRequestSnapshot",
    "ProviderTokenCounter",
    "ReductionResult",
    "ReplayEligibility",
    "RequestBudgetEstimator",
    "ResolvedContextPolicy",
    "ResolvedSessionContext",
    "RetentionPolicy",
    "RollingContextState",
    "RuntimeContextSnapshot",
    "SessionContextState",
    "SessionRef",
    "SourceKind",
    "SummaryResult",
    "SummaryTaskState",
    "TaskSnapshot",
    "Thresholds",
    "TokenCount",
    "TokenCounter",
    "TokenCounterFactory",
    "TokenCounterSelection",
    "TokenizerSpec",
    "ToolReceipt",
    "TranscriptCursor",
    "TrustLevel",
    "ZoneCaps",
    "ZoneUsage",
    "build_receipt",
    "degrade_to_window",
    "estimate_message_tokens",
    "merge_context_summary",
    "profiles",
    "reduce_tool_outputs",
    "resolve_context_policy",
    "resolve_model_spec",
    "retain_recent_tokens",
    "retain_recent_turns",
    "select_token_counter",
    "validate_active_history",
]
