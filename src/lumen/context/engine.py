"""ContextEngine: the single external Seam for context assembly (M1).

This is the compat facade described in plan §M1: it wraps the proven
:class:`~lumen.context.legacy.ContextManager` behind the stable
``prepare`` / ``commit`` / ``control`` interface so callers (the runtime, the
TUI) depend only on high-level DTOs, never on the summary prompt, the token
estimator or the :class:`ContextSummary` type.

What M1 wires:

* :meth:`ContextEngine.prepare` builds the request reservation internally
  (moved out of the runtime) and returns a :class:`ContextEnvelope` whose
  ``messages`` are provider-ready and whose ``fingerprint`` identifies the
  prepared state for commit verification.
* :meth:`ContextEngine.commit` verifies the envelope fingerprint, is idempotent
  for a repeated commit, and rejects a stale/unknown fingerprint as a session
  sequence conflict. It returns the next active history.
* :meth:`ContextEngine.control` is a minimal stub: ``/context`` returns the last
  prepared budget; ``/compact`` and ``/memory`` return ``unsupported`` and are
  fleshed out in M4/M5.

What M1 deliberately leaves to later milestones: structured blocks and zone
accounting (M2), tool-output receipts (M3), delta checkpoints (M4) and memory
(M5/M6). The envelope's ``blocks``/``checkpoint`` are empty until then, and a
single ``compaction`` compat field carries the legacy
:class:`~lumen.context.legacy.CompactionRecord` so the runtime can keep
populating ``RunOutcome.compaction`` without reaching into the manager.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models import Model

from lumen.config import ContextConfig
from lumen.context.artifacts import ArtifactStore
from lumen.context.assembler import ContextAssembler
from lumen.context.budget import (
    resolve_model_spec,
    select_token_counter,
)
from lumen.context.legacy import (
    CompactionRecord,
    ContextManager,
    ContextSummary,
    RequestBudgetEstimator,
    retain_recent_tokens,
)
from lumen.context.transcript import reduce_tool_outputs
from lumen.context.types import (
    ContextBudgetReport,
)
from lumen.events import RunEvent
from lumen.plan import PlanState

EventSink = Callable[[RunEvent], Awaitable[None]]

#: Opaque handle for the prior compaction summary carried through a request.
#: Aliased (not redefined) so the summary type stays in one place; callers that
#: only pass it through never reference :class:`ContextSummary` by name, which is
#: the M1 acceptance criterion for ``runtime.py``.
PreviousSummary = ContextSummary


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
    size the recent window to leave room for them. M2 replaces the legacy
    byte/4 estimator used here with a provider-aware :class:`ModelContextSpec`
    + :class:`TokenCounter`; until then the engine uses the legacy estimator
    internally.
    """

    instructions: str
    tool_schema_documents: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """A complete prepare transaction's inputs (plan §7).

    ``history`` and ``previous_summary`` are carried on the request for M1's
    stateless-compat engine; the v2 engine will read history from the session
    repository via :class:`SessionRef` and track the summary internally (M4).
    """

    session: SessionRef
    agent: AgentRef
    prompt: str
    task: TaskSnapshot
    runtime: RuntimeContextSnapshot
    history: tuple[ModelMessage, ...]
    previous_summary: PreviousSummary | None = None
    focus: str | None = None
    force_compaction: bool = False


@dataclass(frozen=True, slots=True)
class ContextEnvelope:
    """Provider-ready context for one request (plan §7).

    ``blocks`` and ``checkpoint`` are empty until M2/M4. ``compaction`` is the
    M1-compat bridge carrying the legacy :class:`CompactionRecord` so the
    runtime can populate ``RunOutcome.compaction``; it is removed in M4 when the
    structured checkpoint replaces it.
    """

    messages: tuple[ModelMessage, ...]
    blocks: tuple[Any, ...] = ()
    budget: ContextBudgetReport | None = None
    checkpoint: Any | None = None
    compaction: CompactionRecord | None = None
    #: Tool outputs receipt-ized from the history (M3). Each receipt's
    #: ``artifact_ref`` points into the engine's artifact store.
    tool_receipts: tuple[Any, ...] = ()
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class ContextCommit:
    """Finalise a prepared envelope with the run's new messages (plan §7)."""

    session: SessionRef
    envelope_fingerprint: str
    new_messages: tuple[ModelMessage, ...] = ()
    tool_receipts: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextTransition:
    """The effect of a commit on active state (plan §7).

    ``checkpoint``, ``memory_jobs`` and ``session_records`` are empty until
    M4/M6; ``active_history`` is the only field M1 populates.
    """

    active_history: tuple[ModelMessage, ...]
    checkpoint: Any | None = None
    memory_jobs: tuple[Any, ...] = ()
    session_records: tuple[Any, ...] = ()


# --------------------------------------------------------------------------- #
# Control commands (plan §7, §14). M1 implements /context only.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ContextReportCommand:
    """``/context``: return the last prepared budget (read-only)."""


@dataclass(frozen=True, slots=True)
class ContextCompactCommand:
    """``/compact [focus]``: force a compaction (M4)."""

    focus: str | None = None


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

    Wraps a legacy :class:`ContextManager` for its proven compaction algorithm
    while exposing only the ``prepare``/``commit``/``control`` surface. It is
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
    #: Root directory for the content-addressed artifact store (plan §9.3). When
    #: ``None`` the engine does not spill tool outputs to disk (receipts still
    #: inline head/tail); production wires ``~/.lumen/artifacts``.
    artifact_root: str | None = None
    _manager: ContextManager = field(init=False)
    _estimator: RequestBudgetEstimator = field(default_factory=RequestBudgetEstimator)
    _assembler: ContextAssembler = field(init=False)
    _artifacts: ArtifactStore | None = field(init=False, default=None)
    #: session id -> last prepared envelope, for commit verification.
    _pending: dict[str, ContextEnvelope] = field(default_factory=dict[str, ContextEnvelope])
    #: fingerprint -> committed transition, for commit idempotency.
    _committed: dict[str, ContextTransition] = field(default_factory=dict[str, ContextTransition])

    def __post_init__(self) -> None:
        # ``_internal=True`` suppresses the legacy-manager deprecation warning;
        # the manager is the engine's internal implementation, not an external
        # caller that should migrate.
        self._manager = ContextManager(self.config, self.model, _internal=True)
        spec, estimated = resolve_model_spec(self.model_id or "")
        self._assembler = ContextAssembler(
            window_tokens=spec.context_window_tokens,
            max_output_tokens=spec.max_output_tokens,
            counter=select_token_counter(spec),
            estimated_window=estimated,
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

        reservation = self._estimator.build_reservation(
            prompt=request.prompt,
            instructions=request.runtime.instructions,
            tool_schemas=request.runtime.tool_schema_documents,
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
            keep_recent_full = len(retain_recent_tokens(history_input, self.config.keep_recent_tokens))
            reduced = reduce_tool_outputs(history_input, self._artifacts, keep_recent_full=keep_recent_full)
            history_input = reduced.messages
            receipts = tuple(reduced.receipts)
            for receipt in reduced.receipts:
                if receipt.artifact_ref is not None:
                    self._artifacts.add_hold(receipt.artifact_ref, request.session.id)
        prepared = await self._manager.prepare(
            history_input,
            request.task.plan,
            list(request.task.diagnostics),
            emit,
            previous_summary=request.previous_summary,
            reservation=reservation,
        )
        # Assemble structured blocks + a real budget report (M2). This also
        # runs the fixed-context preflight against the model window with the
        # Chinese-aware counter, so a request whose fixed footprint cannot fit
        # fails here, before the provider call (plan §8.2 #3).
        assembled = self._assembler.assemble(
            instructions=request.runtime.instructions,
            prompt=request.prompt,
            tool_schemas=request.runtime.tool_schema_documents,
            history=prepared.history,
        )
        fingerprint = self._fingerprint(request.session.id, prepared.history)
        envelope = ContextEnvelope(
            messages=tuple(prepared.history),
            blocks=assembled.blocks,
            budget=assembled.budget,
            checkpoint=None,
            compaction=prepared.compaction,
            tool_receipts=receipts,
            fingerprint=fingerprint,
        )
        self._pending[request.session.id] = envelope
        return envelope

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
        cached = self._committed.get(commit.envelope_fingerprint)
        if cached is not None:
            return cached
        transition = ContextTransition(
            active_history=(*prepared.messages, *commit.new_messages),
            checkpoint=None,
            memory_jobs=(),
            session_records=(),
        )
        self._committed[commit.envelope_fingerprint] = transition
        return transition

    # -- control -----------------------------------------------------------

    async def control(self, command: ContextCommand, emit: EventSink) -> ContextControlResult:
        """Low-frequency control entry for ``/context``, ``/compact``, ``/memory``.

        Read-only commands never trigger summarisation or memory generation
        (plan §7). M1 implements the read-only report; compaction and memory
        return ``unsupported`` until M4/M5.
        """

        if isinstance(command, ContextReportCommand):
            return self._report()
        if isinstance(command, ContextCompactCommand):
            return ContextControlResult(status="unsupported", message="/compact is implemented in M4")
        # Remaining union member is ContextMemoryCommand (exhaustive match).
        return ContextControlResult(status="unsupported", message="/memory is implemented in M5")

    # -- internal helpers -------------------------------------------------

    def _safety_tokens(self) -> int:
        """Emergency reserve kept inside the soft limit (1% of the limit)."""

        return max(1, self.config.soft_token_limit // 100)

    def _fingerprint(self, session_id: str, messages: Sequence[ModelMessage]) -> str:
        """Stable sha256 over the session id and the prepared messages.

        Two prepares of the same history for the same session yield the same
        fingerprint, so a repeated commit is recognisable. The serialised form
        uses pydantic-ai's message adapter so tool-call ids and parts are part
        of the digest.
        """

        serialised = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        payload = json.dumps(
            {"session": session_id, "messages": serialised}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _report(self) -> ContextControlResult:
        """Build the ``/context`` result from the last prepared envelope."""

        # Prefer the most recently prepared envelope across sessions; a later
        # milestone keys this to the active session id explicitly.
        envelope = next(reversed(self._pending.values()), None)
        if envelope is None or envelope.budget is None:
            return ContextControlResult(status="ok", message="no context prepared yet", payload={})
        budget = envelope.budget
        return ContextControlResult(
            status="ok",
            message=f"{budget.used_tokens} / {budget.context_window_tokens} tokens",
            payload={
                "used_tokens": budget.used_tokens,
                "context_window_tokens": budget.context_window_tokens,
                "output_reserve_tokens": budget.output_reserve_tokens,
                "estimated": budget.estimated,
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
            },
        )


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
