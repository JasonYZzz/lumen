"""Context budgeting, transient provider-history rendering, and commit control.

``ContextEngine`` budgets and renders provider-visible history, but it is not a
second provider protocol implementation. Pydantic AI remains responsible for
combining native instructions, tool definitions, history, current input, and
provider-specific wire details at each model step.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models import Model

from lumen.config import ContextConfig, ModelSettingsConfig
from lumen.context.artifacts import ArtifactStore
from lumen.context.assembler import ContextAssembler
from lumen.context.budget import (
    TokenCounter,
    TokenCounterFactory,
)
from lumen.context.compaction import (
    CompactionPolicy,
    CompactionThrashState,
    Thresholds,
    degrade_to_window,
)
from lumen.context.instructions import InstructionSource
from lumen.context.legacy import (
    CompactionRecord,
    ContextBudgetExceeded,
    ContextManager,
    ContextReservation,
    ContextSummary,
    SummaryTaskState,
    retain_recent_tokens,
    validate_active_history,
)
from lumen.context.memory import MemoryManager, render_memory_index
from lumen.context.memory.records import MemoryKind, MemoryScope
from lumen.context.profiles import ResolvedContextPolicy, resolve_context_policy
from lumen.context.rendering import (
    render_context_data,
    render_history_summary,
    render_session_policy_context,
)
from lumen.context.transcript import reduce_tool_outputs
from lumen.context.types import (
    CheckpointItem,
    CompactionCheckpointV1,
    CompactionCheckpointV2,
    ContextBlock,
    ContextBudgetReport,
    ContextZone,
    ExactLiteral,
    FileState,
    ModelInputManifest,
    ModelInputSource,
    ObservationState,
    ProviderRequestSnapshot,
    ReplayEligibility,
    RollingContextState,
    TranscriptCursor,
)
from lumen.events import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    RunEvent,
)
from lumen.plan import PlanState

EventSink = Callable[[RunEvent], Awaitable[None]]

#: Opaque handle for the prior compaction summary carried through a request.
#: Aliased (not redefined) so the summary type stays in one place; callers that
#: only pass it through never reference :class:`ContextSummary` by name, which is
#: the M1 acceptance criterion for ``runtime.py``.
PreviousSummary = ContextSummary


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _safe_manifest_label(value: str, *, limit: int = 512) -> str:
    """Keep ordinary source labels readable without persisting credential-like URIs."""

    lowered = value.casefold()
    sensitive_markers = ("://", "?", "token=", "key=", "secret=", "password=", "credential=")
    if any(marker in lowered for marker in sensitive_markers):
        return f"redacted:{_canonical_digest(value)}"
    return value[:limit]


def _message_id(message: ModelMessage, sequence: int) -> str:
    payload = json.dumps(
        ModelMessagesTypeAdapter.dump_python([message], mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(f"{sequence}:".encode() + payload.encode()).hexdigest()
    return f"msg-{digest[:16]}"


def _messages_digest(messages: Sequence[ModelMessage]) -> str:
    payload = json.dumps(
        ModelMessagesTypeAdapter.dump_python(list(messages), mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _checkpoint_item(kind: str, value: str, evidence: tuple[str, ...]) -> CheckpointItem:
    digest = hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()[:16]
    return CheckpointItem(
        id=f"{kind}-{digest}",
        text=value,
        source_event_ids=evidence,
        confidence=0.8,
    )


def _extract_exact_literals(
    summary: ContextSummary,
    history: Sequence[ModelMessage],
    evidence: tuple[str, ...],
) -> tuple[ExactLiteral, ...]:
    """Extract literals that a later rolling merge must preserve verbatim."""

    rendered = "\n".join(
        [
            *summary.important_files,
            *summary.key_facts,
            *summary.constraints,
            json.dumps(
                ModelMessagesTypeAdapter.dump_python(list(history), mode="json"),
                ensure_ascii=False,
                default=str,
            ),
        ]
    )
    patterns: tuple[tuple[str, str], ...] = (
        ("url", r"https?://[^\s\"'<>]+"),
        ("path", r"(?<![\w.-])(?:/[^\s\"'<>:,]+|[\w.-]+/[\w./-]+\.[A-Za-z0-9]+)"),
        ("command", r"`([^`\n]+)`"),
        ("error_code", r"\b(?:ERR_[A-Z0-9_]+|E\d{3,}|[A-Z][A-Z0-9_]+-\d+)\b"),
        ("version", r"\bv?\d+\.\d+(?:\.\d+)?(?:[-+][\w.-]+)?\b"),
    )
    found: dict[tuple[str, str], ExactLiteral] = {}
    for kind, pattern in patterns:
        for match in re.finditer(pattern, rendered):
            value = match.group(1) if kind == "command" else match.group(0)
            key = (kind, value)
            found.setdefault(
                key,
                ExactLiteral(kind=cast(Any, kind), value=value, source_event_ids=evidence),
            )
    return tuple(found.values())


def _summary_prefix(summary: ContextSummary) -> ModelRequest:
    lines = ["先前对话摘要:"]
    for label, items in (
        ("目标", summary.goals),
        ("约束", summary.constraints),
        ("已完成", summary.completed),
        ("当前计划", summary.current_plan),
        ("重要文件", summary.important_files),
        ("关键事实", summary.key_facts),
        ("失败与审批", summary.failures_and_approvals),
        ("待处理", summary.outstanding),
    ):
        if items:
            lines.append(f"{label}:")
            lines.extend(f"  - {item}" for item in items)
    return ModelRequest(
        parts=[SystemPromptPart(content=render_history_summary("\n".join(lines)))],
        metadata={"lumen_context_zone": "history_summary", "lumen_summary_version": 1},
    )


def _checkpoint_summary(
    checkpoint: CompactionCheckpointV1 | CompactionCheckpointV2 | None,
) -> ContextSummary | None:
    if not isinstance(checkpoint, CompactionCheckpointV2):
        return None
    state = checkpoint.rolling_state
    literal_facts = [f"{item.kind}: {item.value}" for item in state.exact_literals]
    return ContextSummary(
        goals=list(state.goals),
        constraints=list(state.constraints),
        completed=list(state.completed),
        current_plan=list(state.current_plan),
        important_files=list(state.important_files),
        key_facts=list(dict.fromkeys([*state.key_facts, *literal_facts])),
        failures_and_approvals=list(state.failures_and_approvals),
        outstanding=list(state.outstanding),
    )


class ContextSequenceError(RuntimeError):
    """Raised when a commit references an unknown or stale prepared envelope.

    Maps to the plan §7 invariant: ``commit`` may only receive an envelope this
    engine prepared, and a repeated commit must be idempotent.
    """


# --------------------------------------------------------------------------- #
# High-level DTOs (plan §7). M1 populates the compat subset only.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionRef:
    """Stable reference to the session this request belongs to."""

    id: str


@dataclass(frozen=True, slots=True)
class AgentRef:
    """The agent identity preparing the context."""

    name: str


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Current task state surfaced to the summariser.

    ``diagnostics`` is empty at prepare time (the run has not produced tool
    diagnostics yet); it is carried for forward compatibility with M4's delta
    checkpoint, which will summarise the post-checkpoint diagnostics.
    """

    plan: PlanState
    diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeContextSnapshot:
    """The request's fixed footprint the engine must reserve around.

    Carries the system instructions and tool schema documents so the engine can
    size the recent window with the resolved model policy and its token counter.
    """

    instructions: str
    system_instructions: str = ""
    policy_instructions: str = ""
    instruction_sources: tuple[InstructionSource, ...] = ()
    runtime_context: str = ""
    skill_catalog_documents: tuple[dict[str, Any], ...] = ()
    prompt_mode: str = "legacy"
    prompt_preset: str | None = None
    prompt_version: str = "legacy"
    tool_schema_documents: tuple[dict[str, Any], ...] = ()
    active_skill_documents: tuple[dict[str, Any], ...] = ()
    retrieved_context_documents: tuple[dict[str, Any], ...] = ()
    work_product_documents: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """A complete prepare transaction's inputs (plan §7).

    ``history`` is provider-visible active history; ``source_history`` is the
    append-only transcript used by V2 checkpoint cursors. ``previous_summary``
    remains the compatibility rendering of the authoritative rolling state.
    """

    session: SessionRef
    agent: AgentRef
    prompt: str
    task: TaskSnapshot
    runtime: RuntimeContextSnapshot
    history: tuple[ModelMessage, ...]
    #: Append-only raw transcript. When present, checkpoint deltas are selected
    #: from this sequence by absolute cursor rather than from provider-visible
    #: compacted history.
    source_history: tuple[ModelMessage, ...] = ()
    previous_summary: PreviousSummary | None = None
    #: Structured predecessor and the size of its provider-visible compacted
    #: prefix. Messages after this boundary are the only unsummarized delta.
    previous_checkpoint: CompactionCheckpointV1 | CompactionCheckpointV2 | None = None
    compacted_prefix_length: int = 0
    #: Absolute append-only message offset already covered by the previous
    #: checkpoint. Used to migrate legacy summaries that have no V1 metadata.
    source_offset: int = 0
    focus: str | None = None
    force_compaction: bool = False


@dataclass(frozen=True, slots=True)
class ContextEnvelope:
    """Provider-ready context for one request (plan §7).

    ``blocks`` expose the assembled zones and ``checkpoint`` links successful
    delta compactions. ``compaction`` remains the persistence bridge carrying
    the structured summary and provider-visible compacted prefix.
    """

    provider_history: tuple[ModelMessage, ...]
    blocks: tuple[ContextBlock, ...] = ()
    budget: ContextBudgetReport | None = None
    checkpoint: Any | None = None
    compaction: CompactionRecord | None = None
    #: Tool outputs receipt-ized from the history (M3). Each receipt's
    #: ``artifact_ref`` points into the engine's artifact store.
    tool_receipts: tuple[Any, ...] = ()
    #: Canonical session history excludes stable blocks re-injected for this
    #: provider request (memory and active skill bodies).
    canonical_history: tuple[ModelMessage, ...] = field(default=(), repr=False)
    request_snapshot: ProviderRequestSnapshot | None = None
    fingerprint: str = ""
    #: A request-boundary checkpoint may already cover part of this run.
    #: This cursor is derived from the checkpoint on Session replay.
    covered_new_messages: int = 0

    @property
    def messages(self) -> tuple[ModelMessage, ...]:
        """Deprecated compatibility alias for one release cycle."""

        return self.provider_history


@dataclass(frozen=True, slots=True)
class ContextCommit:
    """Finalise a prepared envelope with the run's new messages (plan §7)."""

    session: SessionRef
    envelope_fingerprint: str
    new_messages: tuple[ModelMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextTransition:
    """The canonical active history produced by a successful commit."""

    active_history: tuple[ModelMessage, ...]


@dataclass(frozen=True, slots=True)
class BackgroundCompactionCandidate:
    """Unpublished compaction prepared after a completed high-pressure turn."""

    source_end: int
    source_digest: str
    parent_checkpoint_id: str | None
    compaction: CompactionRecord
    checkpoint: CompactionCheckpointV2


# --------------------------------------------------------------------------- #
# Control commands (plan §7, §14). M1 implements /context only.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ContextReportCommand:
    """``/context``: return the last prepared budget (read-only)."""

    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextCompactCommand:
    """``/compact [focus]``: force a compaction on the next turn (M4)."""

    focus: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextMemoryCommand:
    """``/memory ...``: recall/remember/forget (M5)."""

    action: str
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


ContextCommand = ContextReportCommand | ContextCompactCommand | ContextMemoryCommand


@dataclass(frozen=True, slots=True)
class ContextControlResult:
    """Result of a low-frequency control command.

    ``status`` is ``ok`` for the read-only report, ``unsupported`` for commands
    whose full implementation lands in a later milestone, and ``error`` for
    failures. ``payload`` carries the budget report for ``/context``.
    """

    status: Literal["ok", "unsupported", "error"]
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


@dataclass
class ContextEngine:
    """The single external Seam for context assembly (plan §5, §7).

    Uses :class:`ContextManager` only as a tool-free structured summarizer while
    owning all counting, thresholds, recent-window retention and preflight. It
    exposes only the ``prepare``/``commit``/``control`` surface and is
    stateful across a prepare->commit pair: it remembers the last prepared
    envelope per session so ``commit`` can verify the fingerprint and stay
    idempotent, and it remembers committed transitions so a repeated commit is a
    no-op.
    """

    config: ContextConfig
    model: Model | str
    #: Model id (``provider:name``) for :class:`ModelContextSpec` resolution.
    #: When unknown, the spec falls back to a conservative window flagged
    #: ``estimated`` so ``/context`` can mark it.
    model_id: str | None = None
    #: Complete model configuration. When supplied, capability resolution uses
    #: its model-family profile and explicit overrides rather than the transport
    #: prefix in ``model_id``.
    model_config: ModelSettingsConfig | None = None
    #: Root directory for the content-addressed artifact store (plan §9.3). When
    #: ``None`` the engine does not spill tool outputs to disk (receipts still
    #: inline head/tail); production wires ``~/.lumen/artifacts``.
    artifact_root: str | None = None
    #: Durable memory manager (M5). When ``None``, ``/memory`` reports
    #: unavailable; production wires a SQLite-backed manager.
    memory: MemoryManager | None = None
    _manager: ContextManager = field(init=False)
    _counter: TokenCounter = field(init=False)
    _assembler: ContextAssembler = field(init=False)
    _artifacts: ArtifactStore | None = field(init=False, default=None)
    _policy: CompactionPolicy = field(default_factory=CompactionPolicy)
    _thresholds: Thresholds = field(init=False)
    _resolved_policy: ResolvedContextPolicy = field(init=False)
    _counter_adapter: str = field(init=False, default="conservative-cjk")
    _counter_fallback_reason: str | None = field(init=False, default=None)
    #: session id -> last prepared envelope, for commit verification.
    _pending: dict[str, ContextEnvelope] = field(default_factory=dict[str, ContextEnvelope])
    _pending_requests: dict[str, ContextRequest] = field(default_factory=dict[str, ContextRequest])
    #: fingerprint -> committed transition, for commit idempotency.
    _committed: dict[str, tuple[str, str, ContextTransition]] = field(
        default_factory=dict[str, tuple[str, str, ContextTransition]]
    )
    #: session id -> anti-thrash state (plan §9.1).
    _thrash: dict[str, CompactionThrashState] = field(default_factory=dict[str, CompactionThrashState])
    #: session ids whose next prepare should force a compaction (``/compact``).
    _force_sessions: set[str] = field(default_factory=set[str])
    #: Committed checkpoint state for continuous runs. Resume callers restore
    #: the same values from the append-only session repository.
    _last_checkpoints: dict[str, CompactionCheckpointV1 | CompactionCheckpointV2] = field(
        default_factory=dict[str, CompactionCheckpointV1 | CompactionCheckpointV2]
    )
    _compacted_prefix_lengths: dict[str, int] = field(default_factory=dict[str, int])
    #: Actual per-step snapshots captured by the Pydantic AI request hook.
    _request_snapshots: dict[str, ProviderRequestSnapshot] = field(
        default_factory=dict[str, ProviderRequestSnapshot]
    )
    _request_input_estimates: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    _usage_drift: dict[str, dict[str, float | int | bool]] = field(
        default_factory=dict[str, dict[str, float | int | bool]]
    )
    _checkpoint_hits: dict[str, int] = field(default_factory=dict[str, int])
    _background_candidates: dict[str, BackgroundCompactionCandidate] = field(
        default_factory=dict[str, BackgroundCompactionCandidate]
    )
    _background_tasks: dict[str, asyncio.Task[None]] = field(
        default_factory=dict[str, asyncio.Task[None]]
    )
    _background_status: dict[str, str] = field(default_factory=dict[str, str])
    _last_compaction_reason: dict[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        # ``_internal=True`` suppresses the legacy-manager deprecation warning;
        # the manager is the engine's internal implementation, not an external
        # caller that should migrate.
        resolved_model = self.model_config or ModelSettingsConfig(id=self.model_id or "unknown:model")
        self._resolved_policy = resolve_context_policy(resolved_model, self.config)
        # The legacy manager remains an internal summarizer implementation. Its
        # direct-call compatibility defaults are replaced with the resolved
        # values, while ContextEngine is the sole compaction decision maker.
        manager_config = self.config.model_copy(
            update={
                "soft_token_limit": self._resolved_policy.soft_limit_tokens,
                "keep_recent_tokens": self._resolved_policy.keep_recent_tokens,
            }
        )
        self._manager = ContextManager(manager_config, self.model, _internal=True)
        selection = TokenCounterFactory.create(self._resolved_policy.tokenizer)
        self._counter = selection.counter
        self._counter_adapter = selection.adapter
        self._counter_fallback_reason = selection.fallback_reason
        self._assembler = ContextAssembler(
            window_tokens=self._resolved_policy.context_window_tokens,
            max_output_tokens=self._resolved_policy.output_reserve_tokens,
            counter=self._counter,
            output_reserve_tokens=self._resolved_policy.output_reserve_tokens,
            estimated_window=self._resolved_policy.estimated,
            soft_limit_tokens=self._resolved_policy.soft_limit_tokens,
            hard_limit_tokens=self._resolved_policy.hard_limit_tokens,
            target_tokens=self._resolved_policy.target_tokens,
        )
        self._thresholds = Thresholds(
            soft=self._resolved_policy.soft_limit_tokens,
            hard=self._resolved_policy.hard_limit_tokens,
            target=self._resolved_policy.target_tokens,
            emergency_reserve=self._resolved_policy.output_reserve_tokens,
        )
        if self.artifact_root is not None:
            self._artifacts = ArtifactStore(self.artifact_root)

    # -- prepare -----------------------------------------------------------

    async def prepare(self, request: ContextRequest, emit: EventSink) -> ContextEnvelope:
        """Assemble a provider-ready envelope, compacting if needed.

        Builds the request reservation internally (prompt + instructions + tool
        schemas + safety margin) so the caller never sizes the window itself.
        Records the envelope under the session so the matching ``commit`` can
        verify it.
        """

        self._request_input_estimates[request.session.id] = []
        self._checkpoint_hits[request.session.id] = sum(
            1
            for document in request.runtime.retrieved_context_documents
            if document.get("server") == "session-checkpoints"
        )
        reservation = ContextReservation(
            prompt_tokens=self._counter.count_messages(
                [ModelRequest(parts=[UserPromptPart(content=request.prompt)])]
            ).tokens,
            instructions_tokens=self._counter.count_messages(
                [ModelRequest(parts=[SystemPromptPart(content=request.runtime.instructions)])]
            ).tokens,
            tool_schema_tokens=self._counter.count_tools(
                request.runtime.tool_schema_documents
            ).tokens,
            safety_tokens=self._safety_tokens(),
        )
        # M3: reduce bulky/empty/duplicate tool outputs to receipts before
        # compaction, so a 50 MB build log cannot crowd the window for the whole
        # session (plan §9.3, §3.2 #8). The recent window compaction keeps is
        # left verbatim (keep_recent_full = the retain-recent cut), so the model
        # still sees full tool outputs where it matters.
        history_input: list[ModelMessage] = list(request.history)
        receipts: tuple[Any, ...] = ()
        if self._artifacts is not None:
            keep_recent_full = len(
                retain_recent_tokens(history_input, self._resolved_policy.keep_recent_tokens)
            )
            reduced = reduce_tool_outputs(history_input, self._artifacts, keep_recent_full=keep_recent_full)
            history_input = reduced.messages
            receipts = tuple(reduced.receipts)
            for receipt in reduced.receipts:
                if receipt.artifact_ref is not None:
                    self._artifacts.add_hold(receipt.artifact_ref, request.session.id)

        # Compaction decision, anti-thrash/cooldown and deterministic safety
        # degradation all use the same resolved policy and counter.
        thrash = self._thrash.setdefault(request.session.id, CompactionThrashState())
        thrash.advance_turn()
        force = request.force_compaction or request.session.id in self._force_sessions
        self._force_sessions.discard(request.session.id)
        over_soft = (
            self._counter.count_messages(history_input).tokens + reservation.total_tokens
            > self._resolved_policy.soft_limit_tokens
        )
        compaction_enabled = self.config.enabled
        previous_checkpoint = request.previous_checkpoint or self._last_checkpoints.get(request.session.id)
        # V2 uses the append-only source transcript as the authority. The
        # compacted provider prefix remains a V1 compatibility projection only.
        if request.source_history:
            source_history = list(request.source_history)
            delta_start = (
                previous_checkpoint.source_end if previous_checkpoint is not None else request.source_offset
            )
            delta_start = min(max(0, delta_start), len(source_history))
            compaction_history = source_history[delta_start:]
            checkpoint_source_start = delta_start
            checkpoint_source_end = len(source_history)
        else:
            source_history = list(history_input)
            requested_boundary = request.compacted_prefix_length
            if requested_boundary == 0 and previous_checkpoint is not None:
                requested_boundary = self._compacted_prefix_lengths.get(request.session.id, 0)
            delta_start = min(max(0, requested_boundary), len(history_input))
            compaction_history = history_input[delta_start:] if delta_start else history_input
            checkpoint_source_start = (
                previous_checkpoint.source_end
                if previous_checkpoint is not None
                else request.source_offset
            )
            checkpoint_source_end = checkpoint_source_start + len(compaction_history)
        prepared_compaction: CompactionRecord | None = None
        checkpoint: CompactionCheckpointV2 | None = None
        candidate = self._background_candidates.pop(request.session.id, None)
        candidate_valid = candidate is not None and self._candidate_matches(
            candidate,
            request.source_history,
            previous_checkpoint,
        )
        if candidate is not None and not candidate_valid:
            self._background_status[request.session.id] = "stale-discarded"
        if compaction_enabled and candidate_valid and candidate is not None and not force:
            self._last_compaction_reason[request.session.id] = "background-candidate"
            prepared_compaction = candidate.compaction
            checkpoint = candidate.checkpoint
            active_history = list(candidate.compaction.active_history)
            thrash.record_success()
            self._background_status[request.session.id] = "adopted"
        elif (
            compaction_enabled
            and over_soft
            and (thrash.thrashed(self._policy) or thrash.cooling_down())
            and not force
        ):
            self._last_compaction_reason[request.session.id] = (
                "failure-cooldown" if thrash.cooling_down() else "anti-thrash"
            )
            # Anti-thrash: stop re-calling the summariser; the safety net below
            # deterministically shrinks instead (plan §9.1).
            active_history: list[ModelMessage] = history_input
        else:
            self._last_compaction_reason[request.session.id] = (
                "disabled"
                if not compaction_enabled
                else "manual"
                if force
                else "soft-limit"
                if over_soft
                else "none"
            )
            prepared_compaction = (
                await self._summarize_delta(
                    compaction_history,
                    request,
                    reservation,
                    emit,
                    previous_checkpoint=previous_checkpoint,
                )
                if compaction_enabled and compaction_history and (over_soft or force)
                else None
            )
            active_history = (
                list(prepared_compaction.active_history)
                if prepared_compaction is not None
                else history_input
            )
            if prepared_compaction is not None and not force:
                thrash.record_auto_compaction()
            if prepared_compaction is not None:
                thrash.record_success()
                checkpoint = self._build_checkpoint(
                    request,
                    compaction_history,
                    prepared_compaction,
                    previous_checkpoint=previous_checkpoint,
                    source_start=checkpoint_source_start,
                    source_end=checkpoint_source_end,
                )
                prepared_compaction = replace(prepared_compaction, checkpoint=checkpoint)
            elif compaction_enabled and compaction_history and (over_soft or force):
                thrash.record_failure(self._policy)

        # M4 safety net: whatever the legacy manager returned, if it is over the
        # hard threshold (model window) the engine degrades deterministically -
        # never sending an over-limit history to the provider (plan §9.4). This
        # replaces "summary failure returns the original over-limit history".
        counter = self._assembler.counter
        fixed_tokens = max(0, reservation.total_tokens - self._resolved_policy.output_reserve_tokens)
        if (
            counter.count_messages(active_history).tokens
            + fixed_tokens
            + self._resolved_policy.output_reserve_tokens
            > self._thresholds.hard
        ):
            active_history = degrade_to_window(
                active_history,
                window_tokens=self._thresholds.hard,
                fixed_tokens=fixed_tokens,
                output_reserve=self._resolved_policy.output_reserve_tokens,
                counter=counter,
                store=self._artifacts,
                keep_recent_tokens=self._resolved_policy.keep_recent_tokens,
            )
            # Preserve a successful summary/checkpoint while replacing only
            # its recent tail. Failed compactions already carry neither value.
            if prepared_compaction is not None:
                prepared_compaction = replace(
                    prepared_compaction,
                    active_history=list(active_history),
                )

        # Assemble structured blocks + a real budget report (M2). This also
        # runs the fixed-context preflight against the model window with the
        # active model counter, so a request whose fixed footprint cannot fit
        # fails here, before the provider call (plan §8.2 #3).
        memory_index, recalled_memory = self._memory_context(request.prompt)
        assembled = self._assembler.assemble(
            instructions=request.runtime.system_instructions or request.runtime.instructions,
            policy=request.runtime.policy_instructions,
            instruction_sources=request.runtime.instruction_sources,
            runtime_context=request.runtime.runtime_context,
            skill_catalog=request.runtime.skill_catalog_documents,
            prompt=request.prompt,
            tool_schemas=request.runtime.tool_schema_documents,
            history=active_history,
            memory_index=memory_index,
            recalled_memory=recalled_memory,
            active_skills=request.runtime.active_skill_documents,
            retrieved_context=request.runtime.retrieved_context_documents,
            task_state=_render_task_state(
                request.task.plan, request.runtime.work_product_documents
            ),
        )
        # Per-zone caps are necessary but not sufficient: their sum plus the
        # recent history and output reserve can still overfill the model. Use
        # the assembler's actual capped fixed footprint for one final,
        # deterministic history reduction, then rebuild the report so every
        # value describes the provider-bound request.
        if assembled.budget.used_tokens > self._thresholds.hard:
            active_history = degrade_to_window(
                active_history,
                window_tokens=self._thresholds.hard,
                fixed_tokens=assembled.fixed_tokens,
                output_reserve=assembled.output_reserve_tokens,
                counter=counter,
                store=self._artifacts,
                keep_recent_tokens=self._resolved_policy.keep_recent_tokens,
            )
            if prepared_compaction is not None:
                prepared_compaction = replace(
                    prepared_compaction,
                    active_history=list(active_history),
                )
            assembled = self._assembler.assemble(
                instructions=request.runtime.system_instructions or request.runtime.instructions,
                policy=request.runtime.policy_instructions,
                instruction_sources=request.runtime.instruction_sources,
                runtime_context=request.runtime.runtime_context,
                skill_catalog=request.runtime.skill_catalog_documents,
                prompt=request.prompt,
                tool_schemas=request.runtime.tool_schema_documents,
                history=active_history,
                memory_index=memory_index,
                recalled_memory=recalled_memory,
                active_skills=request.runtime.active_skill_documents,
                retrieved_context=request.runtime.retrieved_context_documents,
                task_state=_render_task_state(
                    request.task.plan, request.runtime.work_product_documents
                ),
            )
        policy_message = self._policy_message(assembled.blocks)
        context_data_message = self._context_data_message(assembled.blocks)
        # Background data stays low-trust, but must precede the conversation:
        # a refreshed trailing user message can otherwise supersede the task.
        provider_messages = [*policy_message, *context_data_message, *active_history]
        provisional_snapshot = self.snapshot_request(
            session_id=request.session.id,
            model_step=0,
            messages=provider_messages,
            instructions=request.runtime.instructions,
            tool_schemas=request.runtime.tool_schema_documents,
            output_reserve_tokens=assembled.output_reserve_tokens,
        )
        fingerprint = self._fingerprint(request, provider_messages)
        envelope = ContextEnvelope(
            provider_history=tuple(provider_messages),
            blocks=assembled.blocks,
            budget=assembled.budget,
            checkpoint=checkpoint,
            compaction=prepared_compaction,
            tool_receipts=receipts,
            canonical_history=tuple(active_history),
            request_snapshot=provisional_snapshot,
            fingerprint=fingerprint,
        )
        self._pending[request.session.id] = envelope
        self._pending_requests[request.session.id] = request
        return envelope

    async def prepare_step(
        self,
        envelope: ContextEnvelope,
        messages: Sequence[ModelMessage],
        new_messages: Sequence[ModelMessage],
        *,
        session_id: str,
        model_step: int,
        instructions: str,
        tool_schemas: Sequence[dict[str, Any]],
        output_reserve_tokens: int,
        task: TaskSnapshot,
        emit: EventSink,
        runtime_context: str | None = None,
        skill_catalog_documents: Sequence[dict[str, Any]] | None = None,
        active_skill_documents: Sequence[dict[str, Any]] | None = None,
        retrieved_context_documents: Sequence[dict[str, Any]] | None = None,
        work_product_documents: Sequence[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> tuple[ContextEnvelope, list[ModelMessage]]:
        """Compact completed steps inside a run, without publishing a checkpoint.

        The final turn append persists all canonical messages once. Only then
        may confirm_persisted publish the checkpoint. Multiple in-run summaries
        roll into one checkpoint rooted in the last *durable* predecessor.
        """
        request = self._pending_requests.get(session_id)
        if request is None or self._pending.get(session_id) != envelope:
            raise ContextSequenceError("request step does not belong to the active context envelope")
        current = list(messages)
        refreshed_runtime = replace(
            request.runtime,
            runtime_context=(
                request.runtime.runtime_context if runtime_context is None else runtime_context
            ),
            tool_schema_documents=tuple(tool_schemas),
            skill_catalog_documents=(
                request.runtime.skill_catalog_documents
                if skill_catalog_documents is None else tuple(skill_catalog_documents)
            ),
            active_skill_documents=(
                request.runtime.active_skill_documents
                if active_skill_documents is None
                else tuple(active_skill_documents)
            ),
            retrieved_context_documents=(
                request.runtime.retrieved_context_documents
                if retrieved_context_documents is None
                else tuple(retrieved_context_documents)
            ),
            work_product_documents=(
                request.runtime.work_product_documents
                if work_product_documents is None
                else tuple(work_product_documents)
            ),
        )
        if refreshed_runtime != request.runtime or task != request.task:
            request = replace(request, runtime=refreshed_runtime, task=task)
            canonical_current = [message for message in current if _transient_kind(message) is None]
            memory_index, recalled_memory = self._memory_context(request.prompt)
            assembled = self._assembler.assemble(
                instructions=refreshed_runtime.system_instructions or refreshed_runtime.instructions,
                policy=refreshed_runtime.policy_instructions,
                instruction_sources=refreshed_runtime.instruction_sources,
                runtime_context=refreshed_runtime.runtime_context,
                skill_catalog=refreshed_runtime.skill_catalog_documents,
                prompt=request.prompt,
                tool_schemas=refreshed_runtime.tool_schema_documents,
                history=canonical_current,
                memory_index=memory_index,
                recalled_memory=recalled_memory,
                active_skills=refreshed_runtime.active_skill_documents,
                retrieved_context=refreshed_runtime.retrieved_context_documents,
                task_state=_render_task_state(task.plan, refreshed_runtime.work_product_documents),
            )
            current = [
                *self._policy_message(assembled.blocks),
                *self._context_data_message(assembled.blocks),
                *canonical_current,
            ]
            snapshot = self.snapshot_request(
                session_id=session_id,
                model_step=model_step,
                messages=current,
                instructions=instructions,
                tool_schemas=tool_schemas,
                output_reserve_tokens=output_reserve_tokens,
            )
            envelope = replace(
                envelope,
                provider_history=tuple(current),
                blocks=assembled.blocks,
                budget=assembled.budget,
                request_snapshot=snapshot,
                fingerprint=self._fingerprint(request, current),
            )
            self._pending[session_id] = envelope
            self._pending_requests[session_id] = request
        snapshot = self.snapshot_request(
            session_id=session_id, model_step=model_step, messages=current,
            instructions=instructions, tool_schemas=tool_schemas,
            output_reserve_tokens=output_reserve_tokens,
        )
        if not self.config.enabled or model_step <= 1 or (
            not force and snapshot.total_tokens <= self._resolved_policy.soft_limit_tokens
        ):
            return envelope, current
        thrash = self._thrash.setdefault(session_id, CompactionThrashState())
        thrash.advance_turn()
        if not force and (thrash.thrashed(self._policy) or thrash.cooling_down()):
            return envelope, current
        raw_prior = request.source_history or request.history
        source = [*raw_prior, *new_messages]
        durable_parent = request.previous_checkpoint or self._last_checkpoints.get(session_id)
        source_start = durable_parent.source_end if durable_parent is not None else request.source_offset
        previous = (
            envelope.compaction.summary if envelope.compaction is not None
            else _checkpoint_summary(durable_parent) or request.previous_summary
        )
        delta_start = (
            envelope.checkpoint.source_end if envelope.checkpoint is not None else source_start
        )
        if delta_start >= len(source):
            return envelope, current
        delta = source[delta_start:]
        # Reduction is a model projection only. The checkpoint digest below is
        # calculated from raw canonical messages, never from a reduced view.
        if self._artifacts is not None:
            reduced = reduce_tool_outputs(delta, self._artifacts, keep_recent_full=0)
            delta = reduced.messages
            for receipt in reduced.receipts:
                if receipt.artifact_ref is not None:
                    self._artifacts.add_hold(receipt.artifact_ref, session_id)
        await emit(ContextCompactionStarted(source_message_count=len(delta)))
        try:
            result = await self._manager.summarize(
                delta, previous, SummaryTaskState(task.plan, task.diagnostics),
            )
            prefix = _summary_prefix(result.summary)
            # Retain the actual user objective and a coherent suffix of full
            # model/tool batches. A single user turn can contain many batches.
            objective = next(
                (m for m in new_messages if isinstance(m, ModelRequest)
                 and any(isinstance(p, UserPromptPart) for p in m.parts)),
                ModelRequest(parts=[UserPromptPart(content=request.prompt)]),
            )
            tail_source = list(new_messages)
            if self._artifacts is not None:
                tail_source = reduce_tool_outputs(tail_source, self._artifacts, keep_recent_full=0).messages
            transient = [m for m in current if _transient_kind(m) is not None]
            policy = [m for m in transient if _transient_kind(m) == "session-policy-context"]
            data = [m for m in transient if _transient_kind(m) != "session-policy-context"]
            active: list[ModelMessage] | None = None
            provider: list[ModelMessage] = []
            if tail_source == [objective]:
                candidate_provider = [*policy, *data, prefix, objective]
                if self._counter.count_messages(candidate_provider).tokens < snapshot.messages_tokens:
                    active, provider = [prefix, objective], candidate_provider
            for index, message in enumerate(tail_source):
                if not isinstance(message, ModelResponse):
                    continue
                candidate = [prefix, objective, *tail_source[index:]]
                if validate_active_history(candidate):
                    continue
                candidate_provider = [*policy, *data, *candidate]
                candidate_snapshot = self.snapshot_request(
                    session_id=session_id, model_step=model_step, messages=candidate_provider,
                    instructions=instructions, tool_schemas=tool_schemas,
                    output_reserve_tokens=output_reserve_tokens,
                )
                if candidate_snapshot.total_tokens < snapshot.total_tokens:
                    active, provider = candidate, candidate_provider
                if candidate_snapshot.total_tokens <= self._resolved_policy.target_tokens:
                    break
            if active is None:
                raise ContextBudgetExceeded("Cannot compact without losing the latest tool batch")
            usage = dict(envelope.compaction.usage) if envelope.compaction is not None else {}
            for key, value in result.usage.items():
                if isinstance(value, int | float):
                    usage[key] = usage.get(key, 0) + value
            record = CompactionRecord(
                summary=result.summary, active_history=active,
                source_message_count=len(source) - source_start, usage=usage,
            )
            checkpoint = self._build_checkpoint(
                replace(request, task=task), source[source_start:], record,
                previous_checkpoint=durable_parent, source_start=source_start, source_end=len(source),
            )
            record = replace(record, checkpoint=checkpoint)
        except Exception as error:
            thrash.record_failure(self._policy)
            await emit(ContextCompactionFailed(message=str(error)))
            return envelope, current
        thrash.record_auto_compaction()
        thrash.record_success()
        compacted_snapshot = self.snapshot_request(
            session_id=session_id, model_step=model_step, messages=provider,
            instructions=instructions, tool_schemas=tool_schemas,
            output_reserve_tokens=output_reserve_tokens,
        )
        updated = replace(
            envelope, provider_history=tuple(provider), canonical_history=tuple(active),
            checkpoint=checkpoint, compaction=record, covered_new_messages=len(new_messages),
            request_snapshot=compacted_snapshot,
            fingerprint=self._fingerprint(request, provider),
        )
        self._pending[session_id] = updated
        self._last_compaction_reason[session_id] = "request-boundary"
        await emit(ContextCompactionCompleted(
            active_message_count=len(active),
            summary_tokens_estimate=self._counter.count_messages(active).tokens,
        ))
        return updated, provider

    async def _summarize_delta(
        self,
        history: Sequence[ModelMessage],
        request: ContextRequest,
        reservation: ContextReservation,
        emit: EventSink,
        *,
        previous_checkpoint: CompactionCheckpointV1 | CompactionCheckpointV2 | None = None,
    ) -> CompactionRecord | None:
        """Run the pure summarizer, then budget its recent tail in the engine."""

        await emit(ContextCompactionStarted(source_message_count=len(history)))
        try:
            result = await self._manager.summarize(
                history,
                _checkpoint_summary(previous_checkpoint) or request.previous_summary,
                SummaryTaskState(request.task.plan, request.task.diagnostics),
            )
            prefix = _summary_prefix(result.summary)
            summary_tokens = self._counter.count_messages([prefix]).tokens
            available = (
                self._resolved_policy.target_tokens
                - reservation.total_tokens
                - summary_tokens
            )
            recent_budget = max(
                0,
                min(self._resolved_policy.keep_recent_tokens, available),
            )
            active = [prefix, *self._retain_recent(history, recent_budget)]
            errors = validate_active_history(active)
            if errors:
                raise ValueError("; ".join(errors))
        except Exception as error:
            await emit(ContextCompactionFailed(message=str(error)))
            return None
        record = CompactionRecord(
            summary=result.summary,
            active_history=active,
            source_message_count=len(history),
            usage=result.usage,
        )
        await emit(
            ContextCompactionCompleted(
                active_message_count=len(active),
                summary_tokens_estimate=self._counter.count_messages(active).tokens,
            )
        )
        return record

    def _retain_recent(
        self,
        messages: Sequence[ModelMessage],
        budget: int,
    ) -> list[ModelMessage]:
        """Keep the largest coherent suffix that fits with the active counter."""

        if budget <= 0:
            return []
        boundaries: list[int] = []
        for index, message in enumerate(messages):
            if not isinstance(message, ModelRequest):
                continue
            non_system = [part for part in message.parts if not isinstance(part, SystemPromptPart)]
            if non_system and isinstance(non_system[0], UserPromptPart):
                boundaries.append(index)
        for boundary in boundaries:
            candidate = list(messages[boundary:])
            if self._counter.count_messages(candidate).tokens <= budget:
                return candidate if not validate_active_history(candidate) else []
        return []

    def _build_checkpoint(
        self,
        request: ContextRequest,
        history: Sequence[ModelMessage],
        compaction: CompactionRecord,
        *,
        previous_checkpoint: CompactionCheckpointV1 | CompactionCheckpointV2 | None,
        source_start: int,
        source_end: int,
    ) -> CompactionCheckpointV2:
        """Build a structured checkpoint from a successful compaction (plan §10).

        The source range covers exactly the delta supplied to the summariser.
        Its parent and digest make the next delta boundary continuous and
        auditable across ordinary commits and session resume.
        """

        session_id = request.session.id
        parent = previous_checkpoint.checkpoint_id if previous_checkpoint is not None else None
        digest_input = json.dumps(
            ModelMessagesTypeAdapter.dump_python(list(history), mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        objective = "; ".join(compaction.summary.goals) if compaction.summary.goals else ""
        checkpoint_id = (
            f"cp-{hashlib.sha256((session_id + str(source_end) + digest).encode()).hexdigest()[:12]}"
        )
        start_cursor = (
            previous_checkpoint.source_end_cursor
            if isinstance(previous_checkpoint, CompactionCheckpointV2)
            else TranscriptCursor(
                sequence=source_start,
                message_id="session-origin" if source_start == 0 else f"legacy-boundary-{source_start}",
            )
        )
        end_cursor = (
            TranscriptCursor(sequence=source_end, message_id="session-origin")
            if not history
            else TranscriptCursor(
                sequence=source_end,
                message_id=_message_id(history[-1], source_end - 1),
            )
        )
        evidence = (f"transcript:{source_start}:{source_end}",)
        constraints = tuple(
            _checkpoint_item("constraint", text, evidence)
            for text in compaction.summary.constraints
        )
        plan_items = tuple(
            _checkpoint_item("plan", text, evidence) for text in compaction.summary.current_plan
        )
        completed = tuple(
            _checkpoint_item("completed", text, evidence)
            for text in compaction.summary.completed
        )
        approvals: list[CheckpointItem] = []
        failures: list[CheckpointItem] = []
        for text in compaction.summary.failures_and_approvals:
            target = failures if re.search(r"fail|error|denied|失败|错误|拒绝", text, re.I) else approvals
            target.append(_checkpoint_item("failure" if target is failures else "approval", text, evidence))
        files = tuple(
            FileState(
                path=path,
                state=ObservationState.INFERRED,
                detail="preserved from rolling summary",
                source_event_ids=evidence,
            )
            for path in compaction.summary.important_files
        )
        exact_literals = _extract_exact_literals(compaction.summary, history, evidence)
        rolling_state = RollingContextState(
            goals=tuple(compaction.summary.goals),
            constraints=tuple(compaction.summary.constraints),
            completed=tuple(compaction.summary.completed),
            current_plan=tuple(compaction.summary.current_plan),
            important_files=tuple(compaction.summary.important_files),
            key_facts=tuple(compaction.summary.key_facts),
            failures_and_approvals=tuple(compaction.summary.failures_and_approvals),
            outstanding=tuple(compaction.summary.outstanding),
            exact_literals=exact_literals,
        )
        state_json = rolling_state.model_dump_json()
        return CompactionCheckpointV2(
            checkpoint_id=checkpoint_id,
            parent_checkpoint_id=parent,
            source_start=source_start,
            source_end=source_end,
            source_digest=f"sha256:{digest}",
            source_start_cursor=start_cursor,
            source_end_cursor=end_cursor,
            full_history_length=source_end,
            rolling_state=rolling_state,
            state_digest=f"sha256:{hashlib.sha256(state_json.encode('utf-8')).hexdigest()}",
            created_at=datetime.now(UTC),
            focus=request.focus,
            objective=objective,
            constraints=constraints,
            plan=plan_items,
            completed=completed,
            files=files,
            failures=tuple(failures),
            approvals=tuple(approvals),
            next_actions=tuple(
                _checkpoint_item("next", text, evidence)
                for text in compaction.summary.outstanding
            ),
            exact_literals=exact_literals,
        )

    # -- commit ------------------------------------------------------------

    async def commit(self, commit: ContextCommit, emit: EventSink) -> ContextTransition:
        """Finalise a prepared envelope with the run's new messages.

        Verifies the envelope fingerprint matches a prepare this engine produced
        for the session; rejects unknown/stale fingerprints as a sequence
        conflict. A repeated commit for the same fingerprint is idempotent.
        """

        prepared = self._pending.get(commit.session.id)
        if prepared is None:
            raise ContextSequenceError(
                f"no prepared envelope for session {commit.session.id!r}; prepare must precede commit"
            )
        if prepared.fingerprint != commit.envelope_fingerprint:
            raise ContextSequenceError(
                "envelope fingerprint does not match the last prepared envelope "
                f"for session {commit.session.id!r} (stale prepare)"
            )
        # Idempotency: a repeated commit returns the same transition without
        # re-appending the new messages.
        messages_digest = _messages_digest(commit.new_messages)
        cached = self._committed.get(commit.envelope_fingerprint)
        if cached is not None:
            cached_session_id, cached_digest, transition = cached
            if cached_session_id != commit.session.id:
                raise ContextSequenceError("context fingerprint belongs to a different session")
            if cached_digest != messages_digest:
                raise ContextSequenceError(
                    "a repeated commit for the same envelope contained different messages"
                )
            return transition
        transition = ContextTransition(
            active_history=(
                *prepared.canonical_history, *commit.new_messages[prepared.covered_new_messages:],
            ),
        )
        self._committed[commit.envelope_fingerprint] = (
            commit.session.id,
            messages_digest,
            transition,
        )
        return transition

    def reset_session_projection(self, session_id: str) -> None:
        """Discard ephemeral context state after the active history lineage changes."""

        task = self._background_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        self._pending.pop(session_id, None)
        self._pending_requests.pop(session_id, None)
        self._thrash.pop(session_id, None)
        self._force_sessions.discard(session_id)
        self._last_checkpoints.pop(session_id, None)
        self._compacted_prefix_lengths.pop(session_id, None)
        self._request_snapshots.pop(session_id, None)
        self._request_input_estimates.pop(session_id, None)
        self._usage_drift.pop(session_id, None)
        self._checkpoint_hits.pop(session_id, None)
        self._background_candidates.pop(session_id, None)
        self._background_status.pop(session_id, None)
        self._last_compaction_reason.pop(session_id, None)
        stale = [
            fingerprint
            for fingerprint, (owner, _digest, _transition) in self._committed.items()
            if owner == session_id
        ]
        for fingerprint in stale:
            self._committed.pop(fingerprint, None)

    def confirm_persisted(
        self,
        session_id: str,
        envelope_fingerprint: str,
        new_messages: Sequence[ModelMessage],
    ) -> None:
        """Publish checkpoint/background state only after JSONL fsync succeeds."""

        prepared = self._pending.get(session_id)
        if prepared is None or prepared.fingerprint != envelope_fingerprint:
            raise ContextSequenceError(
                f"cannot confirm unknown context envelope for session {session_id!r}"
            )
        if prepared.checkpoint is not None and prepared.compaction is not None:
            self._last_checkpoints[session_id] = prepared.checkpoint
            self._compacted_prefix_lengths[session_id] = len(prepared.compaction.active_history)
        self._maybe_schedule_background(session_id, prepared, new_messages)

    def discard_unpersisted(self, session_id: str, envelope_fingerprint: str) -> None:
        """Forget commit idempotency state when durable append did not happen."""

        prepared = self._pending.get(session_id)
        if prepared is not None and prepared.fingerprint == envelope_fingerprint:
            self._committed.pop(envelope_fingerprint, None)

    def _candidate_matches(
        self,
        candidate: BackgroundCompactionCandidate,
        source_history: Sequence[ModelMessage],
        previous_checkpoint: CompactionCheckpointV1 | CompactionCheckpointV2 | None,
    ) -> bool:
        parent_id = previous_checkpoint.checkpoint_id if previous_checkpoint is not None else None
        return (
            candidate.parent_checkpoint_id == parent_id
            and candidate.source_end == len(source_history)
            and candidate.source_digest == _messages_digest(source_history)
        )

    def _maybe_schedule_background(
        self,
        session_id: str,
        envelope: ContextEnvelope,
        new_messages: Sequence[ModelMessage],
    ) -> None:
        if (
            not self.config.enabled
            or not self.config.background_compaction
            or envelope.compaction is not None
        ):
            return
        request = self._pending_requests.get(session_id)
        if request is None or (request.history and not request.source_history):
            return
        pressure = (
            envelope.budget.used_tokens
            if envelope.budget is not None
            else self._counter.count_messages(envelope.canonical_history).tokens
        ) + self._counter.count_messages(new_messages).tokens
        trigger = int(
            self._resolved_policy.soft_limit_tokens * self.config.background_trigger_ratio
        )
        if pressure < trigger:
            return
        current = self._background_tasks.get(session_id)
        if current is not None and not current.done():
            return
        source_history = (*request.source_history, *new_messages)
        task = asyncio.create_task(
            self._prepare_background_candidate(request, source_history),
            name=f"lumen-context-precompact-{session_id}",
        )
        self._background_tasks[session_id] = task
        self._background_status[session_id] = "running"

    async def _prepare_background_candidate(
        self,
        request: ContextRequest,
        source_history: Sequence[ModelMessage],
    ) -> None:
        session_id = request.session.id

        async def discard_event(_event: RunEvent) -> None:
            return None

        previous = request.previous_checkpoint or self._last_checkpoints.get(session_id)
        source_start = previous.source_end if previous is not None else request.source_offset
        delta = list(source_history[source_start:])
        if not delta:
            self._background_status[session_id] = "idle"
            return
        reservation = ContextReservation(
            prompt_tokens=self._counter.count_messages(
                [ModelRequest(parts=[UserPromptPart(content=request.prompt)])]
            ).tokens,
            instructions_tokens=self._counter.count_messages(
                [ModelRequest(parts=[SystemPromptPart(content=request.runtime.instructions)])]
            ).tokens,
            tool_schema_tokens=self._counter.count_tools(
                request.runtime.tool_schema_documents
            ).tokens,
            safety_tokens=self._safety_tokens(),
        )
        try:
            record = await self._summarize_delta(
                delta,
                request,
                reservation,
                discard_event,
                previous_checkpoint=previous,
            )
            if record is None:
                self._background_status[session_id] = "failed"
                return
            checkpoint = self._build_checkpoint(
                request,
                delta,
                record,
                previous_checkpoint=previous,
                source_start=source_start,
                source_end=len(source_history),
            )
            record = replace(record, checkpoint=checkpoint)
            self._background_candidates[session_id] = BackgroundCompactionCandidate(
                source_end=len(source_history),
                source_digest=_messages_digest(source_history),
                parent_checkpoint_id=(previous.checkpoint_id if previous is not None else None),
                compaction=record,
                checkpoint=checkpoint,
            )
            self._background_status[session_id] = "ready"
        except Exception:
            self._background_status[session_id] = "failed"

    # -- control -----------------------------------------------------------

    async def control(self, command: ContextCommand, emit: EventSink) -> ContextControlResult:
        """Low-frequency control entry for ``/context``, ``/compact``, ``/memory``.

        Read-only commands never trigger summarisation or memory generation.
        """

        if isinstance(command, ContextReportCommand):
            return self._report(command.session_id)
        if isinstance(command, ContextCompactCommand):
            if not self.config.enabled:
                return ContextControlResult(
                    status="unsupported",
                    message="context compaction is disabled by configuration",
                )
            # Force the next prepare for this session to compact (plan §9.1
            # manual trigger). Manual compaction does not count toward the
            # anti-thrash auto limit. Without a session id, force the most
            # recently prepared session.
            session_id = command.session_id or next(reversed(self._pending), None)
            if session_id is None:
                return ContextControlResult(status="ok", message="no active session to compact")
            self._force_sessions.add(session_id)
            focus = f" (focus: {command.focus})" if command.focus else ""
            return ContextControlResult(
                status="ok",
                message=f"compaction scheduled for the next turn{focus}",
            )
        # Remaining union member is ContextMemoryCommand (M5).
        return self._handle_memory(command)

    def _handle_memory(self, command: ContextMemoryCommand) -> ContextControlResult:
        """Dispatch ``/memory`` actions to the memory manager (plan §14.2).

        ``rebuild`` re-renders the Markdown projection from the authority
        without any model call; ``use`` toggles recall (the privacy switch);
        ``remember``/``forget`` are explicit writes/tombstones.
        """

        if self.memory is None:
            return ContextControlResult(status="unsupported", message="memory is not configured")
        action = command.action
        payload = command.payload
        if action == "remember":
            content = str(payload.get("content", "")).strip()
            if not content:
                return ContextControlResult(status="error", message="remember requires content")
            scope = _parse_scope(payload.get("scope"))
            kind = _parse_kind(payload.get("kind"))
            record = self.memory.remember(
                content,
                scope=scope,
                kind=kind,
                session_id=payload.get("session_id"),
            )
            return ContextControlResult(
                status="ok",
                message=f"remembered {record.id} ({record.scope.value})",
                payload={"id": record.id, "status": record.status.value},
            )
        if action == "forget":
            target = str(payload.get("target", "")).strip()
            if not target:
                return ContextControlResult(status="error", message="forget requires a target")
            count = self.memory.forget(target)
            return ContextControlResult(
                status="ok",
                message=f"forgotten {count} record(s)",
                payload={"count": count},
            )
        if action == "edit":
            target = str(payload.get("target", "")).strip()
            if not target:
                return ContextControlResult(status="error", message="edit requires a record id")
            try:
                if bool(payload.get("apply", False)):
                    record = self.memory.apply_edit(target)
                    return ContextControlResult(
                        status="ok",
                        message=f"updated {record.id}",
                        payload={"id": record.id, "status": record.status.value},
                    )
                content = str(payload.get("content", "")).strip()
                if content:
                    record = self.memory.edit_content(target, content)
                    return ContextControlResult(
                        status="ok",
                        message=f"updated {record.id}",
                        payload={"id": record.id, "status": record.status.value},
                    )
                draft, path = self.memory.export_edit(target)
                return ContextControlResult(
                    status="ok",
                    message=(
                        f"edit draft exported to {path}; modify it, then run /memory edit {target} --apply"
                        if path is not None
                        else "edit draft exported; use /memory edit <id> --set <content> to apply"
                    ),
                    payload={"id": target, "draft": draft, "draft_path": str(path) if path else None},
                )
            except (OSError, ValueError) as error:
                return ContextControlResult(status="error", message=str(error))
        if action == "use":
            self.memory.use = bool(payload.get("enabled", True))
            return ContextControlResult(
                status="ok",
                message=f"memory recall {'on' if self.memory.use else 'off'}",
                payload={"use": self.memory.use},
            )
        if action == "learn":
            self.memory.learn = bool(payload.get("enabled", True))
            if self.memory.learn:
                self.memory.start()
            return ContextControlResult(
                status="ok",
                message=f"automatic memory learning {'on' if self.memory.learn else 'off'}",
                payload=self.memory.learning_status(),
            )
        if action == "incognito":
            enabled = bool(payload.get("enabled", True))
            self.memory.set_incognito(enabled)
            return ContextControlResult(
                status="ok",
                message=f"incognito {'on' if enabled else 'off'}",
                payload=self.memory.learning_status(),
            )
        if action == "status":
            return ContextControlResult(
                status="ok",
                message="memory status",
                payload=self.memory.learning_status(),
            )
        if action == "list":
            records = self.memory.list()
            return ContextControlResult(
                status="ok",
                message=f"{len(records)} active record(s)",
                payload={
                    **self.memory.learning_status(),
                    "records": [
                        {"id": r.id, "scope": r.scope.value, "content": r.content, "status": r.status.value}
                        for r in records
                    ],
                },
            )
        if action == "rebuild":
            projection = self.memory.rebuild_projection()
            return ContextControlResult(
                status="ok",
                message="memory index rebuilt from the authority (no model call)",
                payload={"projection": projection},
            )
        return ContextControlResult(status="error", message=f"unknown memory action: {action}")

    # -- internal helpers -------------------------------------------------

    def _safety_tokens(self) -> int:
        """Reserve the complete configured provider output budget."""

        return self._resolved_policy.output_reserve_tokens

    def _fingerprint(self, request: ContextRequest, messages: Sequence[ModelMessage]) -> str:
        """Stable sha256 over the session id and the prepared messages.

        Two prepares of the same history for the same session yield the same
        fingerprint, so a repeated commit is recognisable. The serialised form
        uses pydantic-ai's message adapter so tool-call ids and parts are part
        of the digest.
        """

        serialised = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        payload = json.dumps(
            {
                "session": request.session.id,
                "prompt": request.prompt,
                "instructions": request.runtime.instructions,
                "tool_schemas": request.runtime.tool_schema_documents,
                "active_skills": request.runtime.active_skill_documents,
                "retrieved_context": request.runtime.retrieved_context_documents,
                "work_products": request.runtime.work_product_documents,
                "previous_checkpoint": (
                    request.previous_checkpoint.model_dump(mode="json")
                    if request.previous_checkpoint is not None
                    else None
                ),
                "compacted_prefix_length": request.compacted_prefix_length,
                "source_offset": request.source_offset,
                "messages": serialised,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _memory_context(self, prompt: str) -> tuple[str, str]:
        """Return bounded fixed index and query recall text for one turn."""

        if self.memory is None or not self.memory.use:
            return "", ""
        index_records = self.memory.index()
        index_ids = {record.id for record in index_records}
        recalled = self.memory.recall(prompt, exclude_ids=index_ids)
        index_text = render_memory_index(index_records) if index_records else ""
        recalled_text = "\n".join(
            f"- [{record.id}] ({record.scope.value}, {record.source_kind.value}, "
            f"confidence={record.confidence:.2f}) {record.content}"
            for record in recalled
        )
        return index_text, recalled_text

    @staticmethod
    def _policy_message(
        blocks: Sequence[ContextBlock],
    ) -> list[ModelMessage]:
        rendered = render_session_policy_context(blocks)
        if not rendered:
            return []
        return [
            ModelRequest(
                parts=[SystemPromptPart(content=rendered)],
                metadata={"lumen_transient_context": "session-policy-context"},
            )
        ]

    @staticmethod
    def _context_data_message(blocks: Sequence[ContextBlock]) -> list[ModelMessage]:
        rendered = render_context_data(blocks)
        if not rendered:
            return []
        return [
            ModelRequest(
                parts=[UserPromptPart(content=rendered)],
                metadata={"lumen_transient_context": "context-data"},
            )
        ]

    def snapshot_request(
        self,
        *,
        session_id: str,
        model_step: int,
        messages: Sequence[ModelMessage],
        instructions: str,
        tool_schemas: Sequence[dict[str, Any]],
        output_reserve_tokens: int,
    ) -> ProviderRequestSnapshot:
        """Count one provider-bound model step using the engine's token adapter."""

        names = tuple(str(tool.get("name", "")) for tool in tool_schemas)
        digest = _canonical_digest(list(tool_schemas))
        instruction_tokens = self._assembler.counter.count_text(instructions).tokens
        message_tokens = self._assembler.counter.count_messages(messages).tokens
        tool_tokens = self._assembler.counter.count_tools(tool_schemas).tokens
        snapshot = ProviderRequestSnapshot(
            session_id=session_id,
            model_step=model_step,
            instructions_tokens=instruction_tokens,
            messages_tokens=message_tokens,
            tools_tokens=tool_tokens,
            output_reserve_tokens=output_reserve_tokens,
            total_tokens=instruction_tokens + message_tokens + tool_tokens + output_reserve_tokens,
            context_window_tokens=self._assembler.window_tokens,
            hard_limit_tokens=self._thresholds.hard,
            visible_tools=names,
            visible_tool_digest=digest,
            estimated=self._assembler.estimated_window,
        )
        self._request_snapshots[session_id] = snapshot
        return snapshot

    def build_input_manifest(
        self,
        envelope: ContextEnvelope,
        snapshot: ProviderRequestSnapshot,
        *,
        messages: Sequence[ModelMessage],
        instructions: str,
        tool_schemas: Sequence[dict[str, Any]],
        route: str,
        settings: Mapping[str, Any] | None = None,
        prompt_mode: str = "legacy",
        prompt_preset: str | None = None,
        prompt_version: str = "legacy",
    ) -> ModelInputManifest:
        """Describe one actual provider request without copying source bodies.

        Full history remains in SessionRepository and large/transient bodies
        remain in ArtifactStore. The manifest records ordered references and
        digests so later tooling can prove equality and explain why an old
        request is or is not replayable.
        """

        sources: list[ModelInputSource] = []
        reasons: list[str] = [
            "provider_private_framing_not_captured",
            "intermediate_loop_messages_require_trajectory_capture",
        ]
        durable_session_zones = {
            ContextZone.HISTORY_SUMMARY,
            ContextZone.RECENT_HISTORY,
            ContextZone.CURRENT_INPUT,
            ContextZone.TASK_STATE,
            ContextZone.OUTPUT_RESERVE,
        }
        for order, block in enumerate(envelope.blocks[:128]):
            structured = block.payload.structured or {}
            safe_origin = _safe_manifest_label(block.source.origin)
            raw_artifact_ref = structured.get("body_artifact_ref") or structured.get("artifact_ref")
            artifact_ref = (
                str(raw_artifact_ref)
                if isinstance(raw_artifact_ref, str) and raw_artifact_ref.startswith("sha256:")
                else None
            )
            if artifact_ref is not None:
                reference = artifact_ref
                replayable = True
                reason = None
            elif block.zone in durable_session_zones:
                reference = f"session:{snapshot.session_id}:{block.id}"
                replayable = True
                reason = None
            else:
                reference = safe_origin
                replayable = False
                reason = f"source body has no durable reference: {block.zone.value}"
                reasons.append(f"source_without_durable_reference:{block.zone.value}:{safe_origin}")
            sources.append(
                ModelInputSource(
                    order=order,
                    zone=block.zone,
                    kind=block.source.kind,
                    origin=safe_origin,
                    reference=_safe_manifest_label(reference),
                    revision=_safe_manifest_label(block.source.revision or "", limit=256) or None,
                    content_digest=_canonical_digest(block.payload.model_dump(mode="json")),
                    token_estimate=block.token_estimate,
                    replayable=replayable,
                    non_replayable_reason=reason,
                )
            )
        if len(envelope.blocks) > len(sources):
            reasons.append(f"context_sources_truncated:{len(envelope.blocks) - len(sources)}")

        serialised_messages = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        instructions_digest = _canonical_digest(instructions)
        message_history_digest = _canonical_digest(serialised_messages)
        tool_schema_digest = _canonical_digest(list(tool_schemas))
        settings_digest = _canonical_digest(dict(settings or {}))
        source_payload = [source.model_dump(mode="json") for source in sources]
        context_sources_digest = _canonical_digest(source_payload)
        stable_zones = {
            ContextZone.SYSTEM,
            ContextZone.POLICY,
            ContextZone.CAPABILITY_CATALOG,
        }
        stable_sources = [
            source.model_dump(mode="json") for source in sources if source.zone in stable_zones
        ]
        dynamic_sources = [
            source.model_dump(mode="json") for source in sources if source.zone not in stable_zones
        ]
        stable_prefix_digest = _canonical_digest(
            {
                "instructions": instructions_digest,
                "tools": tool_schema_digest,
                "settings": settings_digest,
                "context_sources": stable_sources,
            }
        )
        dynamic_tail_digest = _canonical_digest(
            {"messages": message_history_digest, "context_sources": dynamic_sources}
        )
        request_fingerprint = _canonical_digest(
            {
                "route": route,
                "stable_prefix": stable_prefix_digest,
                "dynamic_tail": dynamic_tail_digest,
                "output_reserve_tokens": snapshot.output_reserve_tokens,
                "context_fingerprint": envelope.fingerprint,
            }
        )
        provider, separator, model = route.partition(":")
        if not separator:
            provider, model = "unknown", route
        bounded_reasons = tuple(dict.fromkeys(reason[:512] for reason in reasons))[:128]
        return ModelInputManifest(
            session_id=snapshot.session_id[:256],
            step=snapshot.model_step,
            route=route[:512],
            provider=provider[:128],
            model=model[:384],
            prompt_mode=prompt_mode,
            prompt_preset=prompt_preset,
            prompt_version=prompt_version,
            context_fingerprint=envelope.fingerprint[:256],
            message_count=len(messages),
            tool_count=len(tool_schemas),
            instructions_digest=instructions_digest,
            message_history_digest=message_history_digest,
            tool_schema_digest=tool_schema_digest,
            settings_digest=settings_digest,
            context_sources_digest=context_sources_digest,
            stable_prefix_digest=stable_prefix_digest,
            dynamic_tail_digest=dynamic_tail_digest,
            request_fingerprint=request_fingerprint,
            sources=tuple(sources),
            replay_eligibility=(
                ReplayEligibility.VERIFY_ONLY
                if bounded_reasons
                else ReplayEligibility.REPLAYABLE
            ),
            non_replayable_reasons=bounded_reasons,
        )

    def ensure_request_fits(self, snapshot: ProviderRequestSnapshot) -> None:
        """Fail before provider I/O when a real model step crosses the hard limit."""

        if snapshot.total_tokens <= snapshot.hard_limit_tokens:
            self._request_input_estimates.setdefault(snapshot.session_id, []).append(
                max(0, snapshot.total_tokens - snapshot.output_reserve_tokens)
            )
            return
        pressures = sorted(
            (
                ("messages", snapshot.messages_tokens),
                ("tools", snapshot.tools_tokens),
                ("instructions", snapshot.instructions_tokens),
                ("output reserve", snapshot.output_reserve_tokens),
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        detail = ", ".join(f"{name}={tokens}" for name, tokens in pressures[:3])
        raise ContextBudgetExceeded(
            "provider request exceeds hard limit before model call: "
            f"{snapshot.total_tokens} > {snapshot.hard_limit_tokens} tokens ({detail})"
        )

    def observe_provider_usage(self, session_id: str, usage: dict[str, Any]) -> None:
        """Compare completed provider input usage with local preflight estimates."""

        actual_raw = usage.get("input_tokens", usage.get("request_tokens", 0))
        actual = int(actual_raw) if isinstance(actual_raw, int | float) else 0
        estimated = sum(self._request_input_estimates.get(session_id, ()))
        drift = ((estimated - actual) / actual) if actual > 0 else 0.0
        self._usage_drift[session_id] = {
            "estimated_input_tokens": estimated,
            "actual_input_tokens": actual,
            "drift_ratio": round(drift, 4),
            "warning": actual > 0 and abs(drift) >= 0.15,
        }

    def adapt_request_history(
        self,
        envelope: ContextEnvelope,
        messages: Sequence[ModelMessage],
        *,
        instructions: str,
        tool_schemas: Sequence[dict[str, Any]],
        output_reserve_tokens: int,
        session_id: str,
        model_step: int,
    ) -> tuple[list[ModelMessage], ProviderRequestSnapshot]:
        """Bound a real model step without mutating canonical history.

        Large tool results, including results produced in the current run, are
        first projected as content-addressed receipts. If more space is needed,
        only old canonical history is trimmed at complete user-turn boundaries;
        transient context and the live trajectory remain paired and ordered.
        """

        current = list(messages)
        snapshot = self.snapshot_request(
            session_id=session_id,
            model_step=model_step,
            messages=current,
            instructions=instructions,
            tool_schemas=tool_schemas,
            output_reserve_tokens=output_reserve_tokens,
        )
        if snapshot.total_tokens <= snapshot.hard_limit_tokens:
            return current, snapshot
        # A single turn may contain many large tool results after prepare().
        # Receipt-ize those bodies before trimming history so the per-step
        # sliding window applies to the live Loop trajectory as well as old
        # canonical history. Canonical new_messages remain untouched and are
        # still committed in full; this is only a provider-bound projection.
        if self._artifacts is not None:
            reduced = reduce_tool_outputs(current, self._artifacts, keep_recent_full=0)
            if reduced.reduced:
                current = reduced.messages
                for receipt in reduced.receipts:
                    if receipt.artifact_ref is not None:
                        self._artifacts.add_hold(receipt.artifact_ref, session_id)
                snapshot = self.snapshot_request(
                    session_id=session_id,
                    model_step=model_step,
                    messages=current,
                    instructions=instructions,
                    tool_schemas=tool_schemas,
                    output_reserve_tokens=output_reserve_tokens,
                )
                if snapshot.total_tokens <= snapshot.hard_limit_tokens:
                    return current, snapshot
        canonical_count = len(envelope.canonical_history)
        transient_count = 0
        for message in current:
            if _transient_kind(message) is None:
                break
            transient_count += 1
        canonical = current[transient_count : transient_count + canonical_count]
        suffix = current[transient_count + canonical_count :]
        candidate_starts = [0]
        for index, message in enumerate(canonical):
            if not isinstance(message, ModelRequest):
                continue
            non_system = [part for part in message.parts if not isinstance(part, SystemPromptPart)]
            if non_system and isinstance(non_system[0], UserPromptPart):
                candidate_starts.append(index)
        candidate_starts.append(len(canonical))
        for start in sorted(set(candidate_starts)):
            retained = canonical[start:]
            if retained and validate_active_history(retained):
                continue
            candidate = [*current[:transient_count], *retained, *suffix]
            candidate_snapshot = self.snapshot_request(
                session_id=session_id,
                model_step=model_step,
                messages=candidate,
                instructions=instructions,
                tool_schemas=tool_schemas,
                output_reserve_tokens=output_reserve_tokens,
            )
            if candidate_snapshot.total_tokens <= candidate_snapshot.hard_limit_tokens:
                return candidate, candidate_snapshot
        return current, snapshot

    def next_output_reserve(self, current: int, *, observed_output_tokens: int = 0) -> int | None:
        """Grow an implicit output reserve within the resolved capability.

        This is used only after a provider reports a length stop. Explicit
        ``settings.max_tokens`` remains a user-owned cap and is never widened
        by this method's caller. Unknown profiles may grow conservatively up
        to the hard request window; known profiles stop at their architectural
        output maximum.
        """

        baseline = max(current, observed_output_tokens, 1)
        architectural = self._resolved_policy.architectural_max_output_tokens
        ceiling = architectural or max(1, self._thresholds.hard - 1)
        candidate = min(max(baseline * 2, observed_output_tokens + 1_024), ceiling)
        return candidate if candidate > current else None

    def _report(self, session_id: str | None) -> ContextControlResult:
        """Build the ``/context`` result from the last prepared envelope."""

        if session_id is None:
            if len(self._pending) > 1:
                return ContextControlResult(
                    status="error",
                    message="session_id is required when more than one session has context",
                )
            session_id = next(iter(self._pending), None)
        envelope = self._pending.get(session_id) if session_id is not None else None
        if envelope is None or envelope.budget is None:
            return ContextControlResult(status="ok", message="no context prepared yet", payload={})
        budget = envelope.budget
        request_snapshot = self._request_snapshots.get(session_id or "", envelope.request_snapshot)
        thrash = self._thrash.get(session_id or "")
        return ContextControlResult(
            status="ok",
            message=f"{budget.used_tokens} / {budget.context_window_tokens} tokens",
            payload={
                "used_tokens": budget.used_tokens,
                "context_window_tokens": budget.context_window_tokens,
                "output_reserve_tokens": budget.output_reserve_tokens,
                "estimated": budget.estimated,
                "active_model": (
                    self.model_config.id
                    if self.model_config is not None
                    else self.model_id or "unknown:model"
                ),
                "model_profile": self._resolved_policy.profile_id,
                "profile_source": self._resolved_policy.profile_source,
                "estimated_fields": sorted(self._resolved_policy.estimated_fields),
                "tokenizer_adapter": self._counter_adapter,
                "tokenizer_fallback_reason": self._counter_fallback_reason,
                "soft_limit_tokens": self._resolved_policy.soft_limit_tokens,
                "hard_limit_tokens": self._resolved_policy.hard_limit_tokens,
                "target_tokens": self._resolved_policy.target_tokens,
                "keep_recent_tokens": self._resolved_policy.keep_recent_tokens,
                "legacy_overrides": {
                    "soft_token_limit": self._resolved_policy.legacy_soft_override,
                    "keep_recent_tokens": self._resolved_policy.legacy_recent_override,
                },
                "compaction": {
                    "successes": thrash.total_successes if thrash is not None else 0,
                    "failures": thrash.total_failures if thrash is not None else 0,
                    "consecutive_failures": (
                        thrash.consecutive_failures if thrash is not None else 0
                    ),
                    "cooldown": thrash.cooling_down() if thrash is not None else False,
                    "cooldown_until_turn": (
                        thrash.cooldown_until_turn if thrash is not None else 0
                    ),
                },
                "usage_drift": self._usage_drift.get(session_id or "", {}),
                "checkpoint_hits": self._checkpoint_hits.get(session_id or "", 0),
                "last_compaction_reason": self._last_compaction_reason.get(
                    session_id or "",
                    "none",
                ),
                "background_candidate": self._background_status.get(
                    session_id or "",
                    "idle",
                ),
                "checkpoint": (
                    envelope.checkpoint.model_dump(mode="json")
                    if envelope.checkpoint is not None
                    else None
                ),
                "request_snapshot": (
                    request_snapshot.model_dump(mode="json") if request_snapshot is not None else None
                ),
                "zones": [
                    {
                        "zone": zone.zone.value,
                        "tokens": zone.tokens,
                        "share": zone.share,
                        "survival": zone.survival.value,
                    }
                    for zone in budget.zones
                ],
                "pressure": [
                    {"label": item.label, "tokens": item.tokens, "source": item.source}
                    for item in budget.pressure
                ],
                "blocks": [
                    {
                        "id": block.id,
                        "zone": block.zone.value,
                        "origin": block.source.origin,
                        "revision": block.source.revision,
                        "tokens": block.token_estimate,
                        "trust": block.trust.value,
                    }
                    for block in envelope.blocks
                ],
                "capabilities": [
                    {
                        "name": tool.get("name"),
                        "origin": tool.get("origin"),
                        "deferred": bool(tool.get("deferred", False)),
                        "loaded": bool(tool.get("loaded", True)),
                        "tokens": self._assembler.counter.count_tools([tool]).tokens,
                    }
                    for block in envelope.blocks
                    if block.zone.value == "capability_catalog" and block.payload.structured is not None
                    for tool in block.payload.structured.get("tools", [])
                ],
                "active_skills": [
                    block.source.origin.removeprefix("skill:")
                    for block in envelope.blocks
                    if block.source.origin.startswith("skill:")
                ],
                "skill_working_set": [
                    {
                        "name": block.source.origin.removeprefix("skill:"),
                        "tokens": block.token_estimate,
                        "revision": block.source.revision,
                        "status": "included",
                        "complete": True,
                    }
                    for block in envelope.blocks
                    if block.source.origin.startswith("skill:")
                ],
                "omitted_skills": [
                    name
                    for block in envelope.blocks
                    if block.source.origin == "runtime:skill-selection"
                    for name in (block.payload.structured or {}).get("omitted", [])
                ],
                "skill_catalog": next((
                    {**(block.payload.structured or {}), "tokens": block.token_estimate}
                    for block in envelope.blocks if block.source.origin == "runtime:skill-catalog"
                ), dict[str, Any]()),
            },
        )


def _parse_scope(value: object) -> MemoryScope:
    """Parse a scope string from a /memory payload, defaulting to project."""

    if isinstance(value, str):
        try:
            return MemoryScope(value)
        except ValueError:
            return MemoryScope.PROJECT
    return MemoryScope.PROJECT


def _render_task_state(
    plan: PlanState,
    work_product_documents: Sequence[dict[str, Any]] = (),
) -> str:
    """Render the current plan without inventing state outside the controller."""

    lines: list[str] = []
    if plan.steps:
        lines.append(f"计划版本: {plan.revision}")
        for step in plan.steps:
            note = f" — {step.note}" if step.note else ""
            lines.append(f"- [{step.status.value}] `{step.id}` {step.title}{note}")
            for criterion in step.acceptance_criteria:
                lines.append(f"  验收条件 `{criterion.id}`: {criterion.description}")
            if step.evidence_ids:
                lines.append(f"  已关联证据: {', '.join(step.evidence_ids)}")
        if any(step.acceptance_criteria for step in plan.steps):
            lines.append("link_evidence 可用的近期执行回执(不是工作对象 ID):")
            for receipt in plan.evidence[-8:]:
                lines.append(f"- `{receipt.id}` 通过={receipt.passed}: {receipt.summary[:160]}")
    if work_product_documents:
        lines.append("当前工作对象和近期副作用:")
        lines.append(json.dumps(list(work_product_documents), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def _transient_kind(message: ModelMessage) -> str | None:
    raw_metadata = getattr(message, "metadata", None)
    if not isinstance(raw_metadata, dict):
        return None
    metadata = cast(dict[str, Any], raw_metadata)
    value = metadata.get("lumen_transient_context")
    return str(value) if value is not None else None


def _parse_kind(value: object) -> MemoryKind:
    """Parse a kind string from a /memory payload, defaulting to preference."""

    if isinstance(value, str):
        try:
            return MemoryKind(value)
        except ValueError:
            return MemoryKind.PREFERENCE
    return MemoryKind.PREFERENCE


__all__ = [
    "AgentRef",
    "ContextCommit",
    "ContextCompactCommand",
    "ContextControlResult",
    "ContextEngine",
    "ContextEnvelope",
    "ContextMemoryCommand",
    "ContextReportCommand",
    "ContextRequest",
    "ContextSequenceError",
    "ContextTransition",
    "EventSink",
    "PreviousSummary",
    "RuntimeContextSnapshot",
    "SessionRef",
    "TaskSnapshot",
]
