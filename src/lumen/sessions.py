from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_core import to_jsonable_python

from lumen.agents.types import (
    ACTIVE_AGENT_STATUSES,
    AgentEvent,
    AgentMessage,
    AgentResult,
    AgentStatus,
    AgentThreadState,
    SessionAgentState,
)
from lumen.attachments import AttachmentRef
from lumen.collaboration import PlanReviewStatus, SessionSettingsState
from lumen.context.legacy import validate_active_history
from lumen.context.session_state import SessionContextState
from lumen.context.types import ProviderRequestReceipt
from lumen.events import RunStarted, TimelineEventRecord
from lumen.live.types import LiveSessionState, SessionLiveState
from lumen.plan import PlanState
from lumen.work_products import EffectReceipt, SessionWorkState

SCHEMA_VERSION = 11
SUPPORTED_SCHEMA_VERSIONS = tuple(range(1, SCHEMA_VERSION + 1))
SCHEMA_UPGRADE_TARGETS = tuple(range(5, SCHEMA_VERSION + 1))
SESSION_RECORD_TYPES = (
    "agent_event",
    "agent_message",
    "agent_result",
    "agent_thread",
    "context_state",
    "effect",
    "history_rewind",
    "live_session",
    "plan_state",
    "schema_upgrade",
    "session",
    "session_catalog",
    "session_settings",
    "turn",
    "work_state",
)


class SessionCorruptError(ValueError):
    """Raised when persisted session data cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    id: str
    agent_name: str
    model_id: str
    created_at: str
    path: Path


@dataclass(frozen=True, slots=True)
class TurnRecord:
    user_input: str
    messages: list[ModelMessage]
    approvals: list[dict[str, Any]]
    usage: dict[str, Any]
    status: str
    created_at: str
    plan: PlanState = field(default_factory=PlanState)
    diagnostics: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    compaction: dict[str, Any] | None = None
    error_message: str | None = None
    partial_text: str | None = None
    retryable: bool = False
    timeline_events: list[TimelineEventRecord] = field(default_factory=list[TimelineEventRecord])
    recovery_receipts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    channel: str = "text"
    interaction_id: str | None = None
    input_provenance: str | None = None
    provider_item_ids: list[str] = field(default_factory=list[str])
    live_metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    request_receipts: list[ProviderRequestReceipt] = field(
        default_factory=list[ProviderRequestReceipt]
    )
    attachments: list[AttachmentRef] = field(default_factory=list[AttachmentRef])


@dataclass(frozen=True, slots=True)
class TurnPage:
    """A chronological page of turns and the cursor for the preceding page."""

    turns: list[TurnRecord]
    next_before: int | None


class SessionCatalogState(BaseModel):
    """Latest append-only projection for user-managed Session metadata."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(default=None, max_length=80)
    title_generation_turn: int | None = Field(default=None, ge=0)
    archived_at: str | None = None
    deleted_at: str | None = None


@dataclass(frozen=True, slots=True)
class SessionData:
    metadata: SessionMetadata
    turns: list[TurnRecord]
    """Active model history reconstructed for the next run.

    ``history`` reflects what the model should see: it includes the
    compaction summary prefix plus the recent complete turns produced since the
    most recent compaction. ``full_history`` is the raw message history on the
    active conversation lineage and is what gets persisted across compactions.
    Superseded lineages remain in the append-only JSONL journal but never enter
    either projection.
    """
    history: list[ModelMessage]
    full_history: list[ModelMessage]
    catalog: SessionCatalogState = field(default_factory=SessionCatalogState)
    plan: PlanState = field(default_factory=PlanState)
    #: The most recent compaction's summary, materialised from the last turn
    #: that carried a compaction record. Restored on /resume so iterative
    #: compaction continues from the session's own summary rather than a stale
    #: App-level value carried over from a previous session.
    latest_compaction_summary: Any = None
    latest_compaction_checkpoint: Any = None
    compacted_prefix_length: int = 0
    compacted_source_end: int = 0
    context_state: SessionContextState = field(default_factory=SessionContextState)
    settings: SessionSettingsState = field(default_factory=SessionSettingsState)
    work_state: SessionWorkState = field(default_factory=SessionWorkState)
    agent_state: SessionAgentState = field(default_factory=SessionAgentState)
    live_state: SessionLiveState = field(default_factory=SessionLiveState)


def recoverable_orphaned_input(session: SessionData) -> str | None:
    """Recover a pre-fix accepted input only when durable execution evidence exists.

    Older Web sessions persisted the auto-generated title and tool/work facts
    before their terminal turn. The exact transcript cannot be reconstructed,
    but the title is a safe bounded projection of the first accepted prompt.
    """

    if session.turns or session.catalog.title is None:
        return None
    has_execution_evidence = bool(
        session.work_state.effects
        or session.work_state.work_products
        or session.agent_state.threads
        or session.agent_state.events
        or session.agent_state.messages
    )
    return session.catalog.title if has_execution_evidence else None


class SessionRepository:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._projection_cache: dict[Path, tuple[str, SessionData]] = {}

    def clear_projection_cache(self) -> None:
        """Delete the optional read model; canonical JSONL remains untouched."""

        self._projection_cache.clear()

    def _path(self, session_id: str) -> Path:
        try:
            canonical_id = str(UUID(session_id))
        except ValueError as error:
            raise ValueError(f"invalid session id: {session_id}") from error
        return self.directory / f"{canonical_id}.jsonl"

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _append(self, path: Path, record: dict[str, Any], *, create: bool = False) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if create:
            flags |= os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            # Provider, MCP and usage payloads may contain JSON-compatible domain
            # scalars such as Decimal. Normalize through Pydantic's strict JSON
            # encoder at the journal Seam instead of teaching every caller about
            # every provider-specific scalar. Unknown opaque objects still fail
            # serialization; this is deliberately narrower than ``default=str``.
            json_record = to_jsonable_python(record)
            encoded = (
                json.dumps(json_record, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode()
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("session append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def create(self, *, agent_name: str, model_id: str) -> SessionMetadata:
        session_id = str(uuid4())
        path = self._path(session_id)
        created_at = self._now()
        self._append(
            path,
            {
                "type": "session",
                "schema_version": SCHEMA_VERSION,
                "id": session_id,
                "agent_name": agent_name,
                "model_id": model_id,
                "created_at": created_at,
            },
            create=True,
        )
        return SessionMetadata(session_id, agent_name, model_id, created_at, path)

    def fork(self, session_id: str, *, through_turn: int, include_turn: bool = True) -> SessionMetadata:
        """Branch through a turn, or before it when replacing that user message.

        Conversation history is a prefix; existing workspace effects are not undone.
        """

        source = self.load(session_id)
        if through_turn < 0 or through_turn >= len(source.turns):
            raise IndexError(f"turn index out of range: {through_turn}")
        created = self.create(
            agent_name=source.metadata.agent_name,
            model_id=source.metadata.model_id,
        )
        for turn in source.turns[: through_turn + int(include_turn)]:
            self.append_turn(
                created.id,
                user_input=turn.user_input,
                messages=turn.messages,
                approvals=turn.approvals,
                usage=turn.usage,
                status=turn.status,
                plan=turn.plan,
                diagnostics=turn.diagnostics,
                compaction=turn.compaction,
                error_message=turn.error_message,
                partial_text=turn.partial_text,
                retryable=turn.retryable,
                timeline_events=turn.timeline_events,
                recovery_receipts=turn.recovery_receipts,
                channel=turn.channel,
                interaction_id=turn.interaction_id,
                input_provenance=turn.input_provenance,
                provider_item_ids=turn.provider_item_ids,
                live_metadata=turn.live_metadata,
                request_receipts=turn.request_receipts,
                attachments=turn.attachments,
            )
        settings = source.settings
        if not include_turn:
            # A replacement prompt must receive its own plan review, never an
            # execution approval belonging to a discarded conversation suffix.
            settings = settings.model_copy(update={
                "plan_review_status": PlanReviewStatus.NONE,
                "reviewed_revision": None,
                "review_client_request_id": None,
                "execution_run_id": None,
            })
        self.append_session_settings(created.id, settings)
        if source.work_state.work_products or source.work_state.effects:
            self.append_work_state(created.id, source.work_state)
        for thread in source.agent_state.threads:
            copied_ref = thread.ref.model_copy(update={"parent_session_id": created.id})
            if thread.status in ACTIVE_AGENT_STATUSES or thread.status in {
                AgentStatus.APPROVAL_PENDING,
                AgentStatus.IMPORT_PENDING,
                AgentStatus.RECONCILIATION_REQUIRED,
            }:
                copied = thread.model_copy(
                    update={
                        "ref": copied_ref,
                        "status": AgentStatus.NOT_CARRIED,
                        "resolution": "fork_not_carried",
                        "resolution_reason": "active execution is not copied into a fork",
                        "result": None,
                        "updated_at": self._now(),
                    }
                )
            else:
                copied = thread.model_copy(update={"ref": copied_ref})
            self.append_agent_thread(created.id, copied)
        for message in source.agent_state.messages:
            self.append_agent_message(created.id, message)
        return created

    def rewind(self, session_id: str, *, before_turn: int) -> None:
        """Select an earlier conversation prefix without changing Session identity.

        The journal remains append-only: replay treats records written between
        this boundary and the marker as an abandoned lineage. Workspace effects
        are deliberately not rolled back.
        """

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        active = self.load(session_id)
        if before_turn < 0 or before_turn >= len(active.turns):
            raise IndexError(f"turn index out of range: {before_turn}")
        self._ensure_schema(path, 11)
        self._append(
            path,
            {
                "type": "history_rewind",
                "before_turn": before_turn,
                "created_at": self._now(),
            },
        )

    def append_turn(
        self,
        session_id: str,
        *,
        user_input: str,
        messages: Sequence[ModelMessage],
        approvals: list[dict[str, Any]],
        usage: dict[str, Any],
        status: str,
        plan: PlanState | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
        compaction: Any = None,
        error_message: str | None = None,
        partial_text: str | None = None,
        retryable: bool = False,
        timeline_events: Sequence[TimelineEventRecord] = (),
        recovery_receipts: Sequence[dict[str, object]] = (),
        channel: str = "text",
        interaction_id: str | None = None,
        input_provenance: str | None = None,
        provider_item_ids: Sequence[str] = (),
        live_metadata: dict[str, Any] | None = None,
        request_receipts: Sequence[ProviderRequestReceipt] = (),
        attachments: Sequence[AttachmentRef] = (),
    ) -> None:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        record: dict[str, Any] = {
            "type": "turn",
            "user_input": user_input,
            "messages": ModelMessagesTypeAdapter.dump_python(list(messages), mode="json"),
            "approvals": approvals,
            "usage": usage,
            "status": status,
            "created_at": self._now(),
            "plan": (plan or PlanState()).model_dump(mode="json"),
            "diagnostics": list(diagnostics or []),
            "timeline_events": [record.to_dict() for record in timeline_events],
            "recovery_receipts": list(recovery_receipts),
            "channel": channel,
            "provider_item_ids": list(provider_item_ids),
            "live_metadata": dict(live_metadata or {}),
            "request_receipts": [item.model_dump(mode="json") for item in request_receipts],
            "attachments": [item.model_dump(mode="json") for item in attachments],
        }
        if interaction_id is not None:
            record["interaction_id"] = interaction_id
        if input_provenance is not None:
            record["input_provenance"] = input_provenance
        if compaction is not None:
            record["compaction"] = self._dump_compaction(compaction)
        if error_message is not None:
            record["error_message"] = error_message
        if partial_text is not None:
            record["partial_text"] = partial_text
        if retryable:
            record["retryable"] = True
        self._append(path, record)

    def append_turn_started(
        self,
        session_id: str,
        *,
        user_input: str,
        interaction_id: str,
        plan: PlanState | None = None,
        attachments: Sequence[AttachmentRef] = (),
    ) -> None:
        """Persist an accepted input before its run can produce side effects.

        A later terminal turn with the same ``interaction_id`` supersedes this
        running projection without rewriting the append-only journal.
        """

        self.append_turn(
            session_id,
            user_input=user_input,
            messages=[],
            approvals=[],
            usage={},
            status="running",
            plan=plan,
            timeline_events=[
                TimelineEventRecord.from_event(RunStarted(user_input, tuple(attachments)), sequence=1)
            ],
            interaction_id=interaction_id,
            attachments=attachments,
        )

    def append_context_state(self, session_id: str, state: SessionContextState) -> None:
        """Append a v5 source-state snapshot without rewriting prior records."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 5)
        self._append(
            path,
            {
                "type": "context_state",
                "created_at": self._now(),
                "state": state.model_dump(mode="json"),
            },
        )

    def append_session_settings(self, session_id: str, state: SessionSettingsState) -> None:
        """Durably append the session-scoped collaboration/approval state."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 10 if state.reasoning else 6)
        self._append(
            path,
            {
                "type": "session_settings",
                "created_at": self._now(),
                "state": state.model_dump(
                    mode="json", exclude={"reasoning"} if not state.reasoning else set(),
                ),
            },
        )

    def append_session_catalog(self, session_id: str, state: SessionCatalogState) -> None:
        """Append the latest title/archive/delete projection without rewriting history."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 9)
        self._append(
            path,
            {
                "type": "session_catalog",
                "created_at": self._now(),
                "state": state.model_dump(mode="json"),
            },
        )

    def append_work_state(self, session_id: str, state: SessionWorkState) -> None:
        """Append the latest durable work-product snapshot."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 7)
        self._append(
            path,
            {
                "type": "work_state",
                "created_at": self._now(),
                "state": state.model_dump(mode="json"),
            },
        )

    def append_effect(self, session_id: str, effect: EffectReceipt) -> None:
        """Append one write-ahead effect state transition."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 7)
        self._append(
            path,
            {
                "type": "effect",
                "created_at": self._now(),
                "effect": effect.model_dump(mode="json"),
            },
        )

    def append_agent_thread(self, session_id: str, thread: AgentThreadState) -> None:
        """Append the latest materialized state for one Agent Thread."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        if thread.ref.parent_session_id != session_id:
            raise ValueError("agent thread does not belong to session")
        self._ensure_schema(path, 10)
        self._append(
            path,
            {
                "type": "agent_thread",
                "created_at": self._now(),
                "thread": thread.model_dump(mode="json"),
            },
        )

    def append_agent_event(self, session_id: str, event: AgentEvent) -> None:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        if event.session_id != session_id:
            raise ValueError("agent event does not belong to session")
        self._ensure_schema(path, 8)
        self._append(
            path,
            {
                "type": "agent_event",
                "created_at": self._now(),
                "event": event.model_dump(mode="json"),
            },
        )

    def append_agent_message(self, session_id: str, message: AgentMessage) -> None:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 8)
        self._append(
            path,
            {
                "type": "agent_message",
                "created_at": self._now(),
                "message": message.model_dump(mode="json"),
            },
        )

    def append_agent_result(self, session_id: str, result: AgentResult) -> None:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 8)
        self._append(
            path,
            {
                "type": "agent_result",
                "created_at": self._now(),
                "result": result.model_dump(mode="json"),
            },
        )

    def append_live_session(self, session_id: str, state: LiveSessionState) -> None:
        """Append one materialized Live lifecycle transition."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        if state.ref.session_id != session_id:
            raise ValueError("live session does not belong to session")
        self._ensure_schema(path, 9)
        self._append(
            path,
            {
                "type": "live_session",
                "created_at": self._now(),
                "state": state.model_dump(mode="json"),
            },
        )

    def _ensure_schema(self, path: Path, target: int) -> None:
        """Append one non-destructive marker before a record introduced in ``target``."""

        if target not in SCHEMA_UPGRADE_TARGETS:
            raise ValueError(f"unsupported schema upgrade target: {target}")
        effective = self._effective_schema_version(path)
        if effective >= target:
            return
        self._append(
            path,
            {
                "type": "schema_upgrade",
                "from_version": effective,
                "to_version": target,
                "created_at": self._now(),
            },
        )

    @staticmethod
    def _effective_schema_version(path: Path) -> int:
        effective = 0
        with path.open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, 1):
                record = _parse_json_record(line, line_number=line_number, path=path)
                if line_number == 1:
                    effective = int(record.get("schema_version", 0))
                elif record.get("type") == "schema_upgrade":
                    from_version = int(record.get("from_version", 0))
                    to_version = int(record.get("to_version", 0))
                    if (
                        from_version != effective
                        or to_version not in SCHEMA_UPGRADE_TARGETS
                        or to_version <= effective
                    ):
                        raise SessionCorruptError(f"invalid schema_upgrade at line {line_number}")
                    effective = to_version
        return effective

    def append_plan_state(self, session_id: str, state: PlanState) -> None:
        """Persist a plan transition that must survive before the next turn."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._ensure_schema(path, 6)
        self._append(
            path,
            {
                "type": "plan_state",
                "created_at": self._now(),
                "state": state.model_dump(mode="json"),
            },
        )

    def retrieve_checkpoint_episodes(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 3,
    ) -> tuple[dict[str, str], ...]:
        """Lexically retrieve immutable prior checkpoints as untrusted episodes.

        Only the latest rolling summary is injected by default. Older
        checkpoints stay in JSONL and enter a request only when the current
        prompt overlaps their structured state.
        """

        path = self._path(session_id)
        if not path.is_file() or limit <= 0:
            return ()
        terms = _retrieval_terms(query)
        if not terms:
            return ()
        episodes: list[tuple[int, dict[str, str]]] = []
        full_history: list[ModelMessage] = []
        parent: Any = None
        # Read the active lineage projection instead of scanning every raw
        # turn. Abandoned answers and their compaction summaries remain
        # auditable in JSONL but are not retrieval candidates.
        for turn_index, turn in enumerate(self.load(session_id).turns):
            if turn.status not in {"completed", "waiting_for_user"}:
                continue
            compaction = turn.compaction
            if compaction is not None:
                checkpoint = _load_checkpoint(compaction)
                summary = _load_summary(compaction)
                raw_checkpoint: Any = compaction.get("checkpoint")
                is_v2 = (
                    isinstance(raw_checkpoint, dict)
                    and cast(dict[str, Any], raw_checkpoint).get("schema_version") == 2
                )
                if checkpoint is not None and not is_v2:
                    checkpoint = _map_v1_checkpoint(checkpoint, full_history, parent)
                if is_v2 and checkpoint is not None:
                    summary = _summary_from_v2_checkpoint(checkpoint)
                if (
                    checkpoint is not None
                    and summary is not None
                    and (
                        not is_v2
                        or _validate_v2_checkpoint(
                            checkpoint,
                            full_history,
                            parent,
                        )
                    )
                ):
                    checkpoint_id = str(checkpoint.checkpoint_id)
                    episodes.append(
                        (
                            turn_index,
                            {
                                "server": "session-checkpoints",
                                "uri": checkpoint_id,
                                "revision": str(checkpoint.source_digest),
                                "body": summary.model_dump_json(),
                            },
                        )
                    )
                    parent = checkpoint
            full_history.extend(turn.messages)
        # Latest rolling state is injected through HISTORY_SUMMARY; only older
        # validated checkpoints are eligible as immutable episodes.
        candidates: list[tuple[int, int, dict[str, str]]] = []
        for ordinal, document in episodes[:-1]:
            lower = document["body"].lower()
            score = sum(1 for term in terms if term in lower)
            if score:
                candidates.append(
                    (
                        score,
                        ordinal,
                        document,
                    )
                )
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(item[2] for item in candidates[:limit])

    @staticmethod
    def _dump_compaction(compaction: Any) -> dict[str, Any]:
        """Persist only the compacted prefix and the summary, not the source."""

        summary = getattr(compaction, "summary", None)
        active_history = getattr(compaction, "active_history", None)
        source_message_count = getattr(compaction, "source_message_count", 0)
        usage = getattr(compaction, "usage", {}) or {}
        checkpoint = getattr(compaction, "checkpoint", None)
        result = {
            "summary": summary.model_dump(mode="json") if summary is not None else None,
            "active_history": ModelMessagesTypeAdapter.dump_python(list(active_history or []), mode="json"),
            "source_message_count": int(source_message_count),
            "usage": dict(usage),
        }
        if checkpoint is not None:
            result["checkpoint"] = checkpoint.model_dump(mode="json")
        return result

    def load(self, session_id: str) -> SessionData:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        payload = path.read_bytes()
        projection_digest = hashlib.sha256(payload).hexdigest()
        cached = self._projection_cache.get(path)
        if cached is not None and cached[0] == projection_digest:
            return _copy_session_data(cached[1])
        records: list[dict[str, Any]] = []
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SessionCorruptError(f"session {path.name} is not UTF-8") from error
        for line_number, line in enumerate(text.splitlines(), 1):
            try:
                raw_record: Any = json.loads(line)
            except json.JSONDecodeError as error:
                raise SessionCorruptError(f"invalid JSON at line {line_number} in {path.name}") from error
            if not isinstance(raw_record, dict):
                raise SessionCorruptError(f"record at line {line_number} is not an object")
            records.append(cast(dict[str, Any], raw_record))
        if not records or records[0].get("type") != "session":
            raise SessionCorruptError("missing session header")
        header = records[0]
        schema_version = header.get("schema_version")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise SessionCorruptError(f"unsupported session schema: {schema_version}")
        metadata = SessionMetadata(
            id=str(header["id"]),
            agent_name=str(header["agent_name"]),
            model_id=str(header["model_id"]),
            created_at=str(header["created_at"]),
            path=path,
        )
        turns: list[TurnRecord] = []
        running_turn_positions: dict[str, int] = {}
        # ``active_history`` is rebuilt turn-by-turn; whenever a compaction
        # record is present we restart from its compacted prefix so the model
        # doesn't see older turns it has already summarised.
        active_history: list[ModelMessage] = []
        full_history: list[ModelMessage] = []
        latest_plan = PlanState()
        latest_compaction_summary: Any = None
        latest_compaction_checkpoint: Any = None
        compacted_prefix_length = 0
        compacted_source_end = 0
        context_state = SessionContextState()
        settings = SessionSettingsState()
        catalog = SessionCatalogState()
        work_state = SessionWorkState()
        agent_state = SessionAgentState()
        live_state = SessionLiveState()
        effective_schema = int(schema_version)
        for line_number, record in enumerate(records[1:], 2):
            if record.get("type") == "schema_upgrade":
                from_version = int(record.get("from_version", 0))
                to_version = int(record.get("to_version", 0))
                if (
                    from_version != effective_schema
                    or to_version not in SCHEMA_UPGRADE_TARGETS
                    or to_version <= effective_schema
                ):
                    raise SessionCorruptError(f"invalid schema_upgrade at line {line_number}")
                effective_schema = to_version
                continue
            if record.get("type") == "live_session":
                if effective_schema < 9:
                    raise SessionCorruptError(
                        f"live_session is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    live_session = LiveSessionState.model_validate(record.get("state"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid live_session at line {line_number}") from error
                if live_session.ref.session_id != metadata.id:
                    raise SessionCorruptError(f"live_session ownership mismatch at line {line_number}")
                live_state = live_state.upsert(live_session)
                continue
            if record.get("type") == "session_catalog":
                if effective_schema < 9:
                    raise SessionCorruptError(
                        f"session_catalog is not valid for schema v{effective_schema} "
                        f"at line {line_number}"
                    )
                try:
                    next_catalog = SessionCatalogState.model_validate(record.get("state"))
                except ValueError as error:
                    raise SessionCorruptError(
                        f"invalid session_catalog at line {line_number}"
                    ) from error
                # Deletion is terminal. A delayed catalog writer (for example
                # another Host's title generator) cannot resurrect a Session.
                # Still validate every record, even after the tombstone.
                if catalog.deleted_at is None:
                    catalog = next_catalog
                continue
            if record.get("type") == "work_state":
                if effective_schema < 7:
                    raise SessionCorruptError(
                        f"work_state is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    work_state = SessionWorkState.model_validate(record.get("state"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid work_state at line {line_number}") from error
                continue
            if record.get("type") == "effect":
                if effective_schema < 7:
                    raise SessionCorruptError(
                        f"effect is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    effect = EffectReceipt.model_validate(record.get("effect"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid effect at line {line_number}") from error
                work_state = work_state.upsert_effect(effect)
                continue
            if record.get("type") == "agent_thread":
                if effective_schema < 8:
                    raise SessionCorruptError(
                        f"agent_thread is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    thread = AgentThreadState.model_validate(record.get("thread"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid agent_thread at line {line_number}") from error
                if thread.ref.parent_session_id != metadata.id:
                    raise SessionCorruptError(f"agent_thread ownership mismatch at line {line_number}")
                agent_state = agent_state.upsert_thread(thread)
                continue
            if record.get("type") == "agent_event":
                if effective_schema < 8:
                    raise SessionCorruptError(
                        f"agent_event is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    event = AgentEvent.model_validate(record.get("event"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid agent_event at line {line_number}") from error
                if event.session_id != metadata.id:
                    raise SessionCorruptError(f"agent_event ownership mismatch at line {line_number}")
                agent_state = agent_state.append_event(event)
                continue
            if record.get("type") == "agent_message":
                if effective_schema < 8:
                    raise SessionCorruptError(
                        f"agent_message is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    message = AgentMessage.model_validate(record.get("message"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid agent_message at line {line_number}") from error
                agent_state = agent_state.append_message(message)
                continue
            if record.get("type") == "agent_result":
                if effective_schema < 8:
                    raise SessionCorruptError(
                        f"agent_result is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    result = AgentResult.model_validate(record.get("result"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid agent_result at line {line_number}") from error
                existing = agent_state.get(result.agent_id)
                if existing is None:
                    raise SessionCorruptError(f"agent_result references unknown agent at line {line_number}")
                agent_state = agent_state.upsert_thread(
                    existing.model_copy(
                        update={
                            "result": result,
                            "status": result.status,
                            "updated_at": result.created_at,
                        }
                    )
                )
                continue
            if record.get("type") == "context_state":
                if effective_schema < 5:
                    raise SessionCorruptError(
                        f"context_state is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    context_state = SessionContextState.model_validate(record.get("state"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid context_state at line {line_number}") from error
                continue
            if record.get("type") == "session_settings":
                if effective_schema < 6:
                    raise SessionCorruptError(
                        f"session_settings is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    settings = SessionSettingsState.model_validate(record.get("state"))
                    if settings.reasoning and effective_schema < 10:
                        raise ValueError("reasoning snapshots require schema v10")
                except ValueError as error:
                    raise SessionCorruptError(f"invalid session_settings at line {line_number}") from error
                continue
            if record.get("type") == "plan_state":
                if effective_schema < 6:
                    raise SessionCorruptError(
                        f"plan_state is not valid for schema v{effective_schema} at line {line_number}"
                    )
                try:
                    latest_plan = PlanState.model_validate(record.get("state"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid plan_state at line {line_number}") from error
                continue
            if record.get("type") == "history_rewind":
                if effective_schema < 11:
                    raise SessionCorruptError(
                        f"history_rewind is not valid for schema v{effective_schema} "
                        f"at line {line_number}"
                    )
                before_turn = record.get("before_turn")
                if (
                    isinstance(before_turn, bool)
                    or not isinstance(before_turn, int)
                    or not 0 <= before_turn < len(turns)
                ):
                    raise SessionCorruptError(f"invalid history_rewind at line {line_number}")
                turns = turns[:before_turn]
                running_turn_positions = {
                    turn.interaction_id: index
                    for index, turn in enumerate(turns)
                    if turn.interaction_id is not None and turn.status == "running"
                }
                (
                    active_history,
                    full_history,
                    latest_compaction_summary,
                    latest_compaction_checkpoint,
                    compacted_prefix_length,
                    compacted_source_end,
                ) = _project_turn_history(turns)
                latest_plan = turns[-1].plan if turns else PlanState()
                settings = settings.model_copy(
                    update={
                        "plan_review_status": PlanReviewStatus.NONE,
                        "reviewed_revision": None,
                        "review_client_request_id": None,
                        "execution_run_id": None,
                    }
                )
                context_state = context_state.model_copy(
                    update={"active_resources": (), "pending_clarification": None}
                )
                continue
            turn = _parse_turn(record, line_number=line_number)
            messages = turn.messages
            pending_position = (
                running_turn_positions.pop(turn.interaction_id, None)
                if turn.interaction_id is not None and turn.status != "running"
                else None
            )
            if pending_position is None:
                turns.append(turn)
                if turn.interaction_id is not None and turn.status == "running":
                    running_turn_positions[turn.interaction_id] = len(turns) - 1
            else:
                turns[pending_position] = turn
            completed_steps = bool(messages) and any(
                item.get("kind") == "completed_model_steps"
                and item.get("message_count") == len(messages)
                for item in turn.diagnostics
            ) and not validate_active_history(messages)
            if turn.status in {"completed", "waiting_for_user"} or (
                turn.status in {"failed", "cancelled"} and completed_steps
            ):
                covered_messages = 0
                if turn.compaction is not None:
                    candidate = _load_checkpoint(turn.compaction)
                    raw_checkpoint: Any = turn.compaction.get("checkpoint")
                    is_v2_record = (
                        isinstance(raw_checkpoint, dict)
                        and cast(dict[str, Any], raw_checkpoint).get("schema_version") == 2
                    )
                    if candidate is not None and not is_v2_record:
                        candidate = _map_v1_checkpoint(
                            candidate,
                            full_history,
                            latest_compaction_checkpoint,
                        )
                    summary = (
                        _summary_from_v2_checkpoint(candidate)
                        if is_v2_record and candidate is not None
                        else _load_summary(turn.compaction)
                    )
                    valid = not is_v2_record or (
                        candidate is not None
                        and len(full_history) <= candidate.source_end <= len(full_history) + len(messages)
                        and _validate_v2_checkpoint(
                            candidate,
                            [*full_history, *messages][:candidate.source_end],
                            latest_compaction_checkpoint,
                        )
                    )
                    if valid:
                        if is_v2_record and candidate is not None:
                            covered_messages = candidate.source_end - len(full_history)
                        # Reset only from a verified V2 checkpoint (or a V1/
                        # legacy record accepted in read-only compatibility
                        # mode). A corrupt V2 record is ignored and replay
                        # continues from the last legal checkpoint plus JSONL.
                        active_history = _load_active_prefix(turn.compaction)
                        compacted_prefix_length = len(active_history)
                        latest_compaction_summary = summary
                        latest_compaction_checkpoint = candidate
                        compacted_source_end = (
                            candidate.source_end if candidate is not None else len(full_history)
                        )
                full_history.extend(messages)
                active_history.extend(messages[covered_messages:])
            # Model history only accepts completed turns, but the latest plan
            # is diagnostic state and remains useful after failure/cancel.
            latest_plan = turn.plan
        projection = SessionData(
            metadata=metadata,
            turns=turns,
            history=active_history,
            full_history=full_history,
            catalog=catalog,
            plan=latest_plan,
            latest_compaction_summary=latest_compaction_summary,
            latest_compaction_checkpoint=latest_compaction_checkpoint,
            compacted_prefix_length=compacted_prefix_length,
            compacted_source_end=compacted_source_end,
            context_state=context_state,
            settings=settings,
            work_state=work_state,
            agent_state=agent_state,
            live_state=live_state,
        )
        self._projection_cache[path] = (projection_digest, projection)
        return _copy_session_data(projection)

    def load_turn_page(
        self,
        session_id: str,
        *,
        before: int | None = None,
        limit: int = 20,
    ) -> TurnPage:
        """Load a chronological turn page ending before an ordinal cursor.

        ``before=None`` addresses the end of the session. Cursors are stable
        turn ordinals, so prepending a page does not depend on timestamps.
        """

        if limit < 1:
            raise ValueError("turn page limit must be positive")
        # Pagination is a view of the same active-lineage projection used by
        # model context. Keeping one projection authority prevents abandoned
        # branches from reappearing only in the UI after resume.
        turns = self.load(session_id).turns
        turn_count = len(turns)
        end = turn_count if before is None else before
        if not 0 <= end <= turn_count:
            raise ValueError(f"invalid turn page cursor: {before}")
        start = max(0, end - limit)
        return TurnPage(turns=turns[start:end], next_before=start if start > 0 else None)

    def list(self) -> list[SessionMetadata]:
        sessions: list[SessionMetadata] = []
        paths = sorted(
            self.directory.glob("*.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in paths:
            try:
                sessions.append(self.load(path.stem).metadata)
            except (OSError, ValueError):
                continue
        return sessions

    def artifact_references(self) -> set[str]:
        """Scan durable session records for content-addressed artifact refs."""

        references: set[str] = set()
        for metadata in self.list():
            with metadata.path.open(encoding="utf-8") as file:
                for line_number, line in enumerate(file, 1):
                    record = _parse_json_record(line, line_number=line_number, path=metadata.path)
                    _collect_artifact_refs(record, references)
        return references


def _parse_json_record(line: str, *, line_number: int, path: Path) -> dict[str, Any]:
    try:
        raw_record: Any = json.loads(line)
    except json.JSONDecodeError as error:
        raise SessionCorruptError(f"invalid JSON at line {line_number} in {path.name}") from error
    if not isinstance(raw_record, dict):
        raise SessionCorruptError(f"record at line {line_number} is not an object")
    return cast(dict[str, Any], raw_record)


def _copy_session_data(value: SessionData) -> SessionData:
    """Return a caller-owned projection shell around immutable domain state."""

    return replace(
        value,
        turns=list(value.turns),
        history=list(value.history),
        full_history=list(value.full_history),
    )


def _collect_artifact_refs(value: object, output: set[str]) -> None:
    if isinstance(value, str):
        if value.startswith("sha256:") and len(value) == 71:
            output.add(value)
        return
    if isinstance(value, dict):
        for item in cast(dict[object, object], value).values():
            _collect_artifact_refs(item, output)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            _collect_artifact_refs(item, output)


def _parse_turn(record: dict[str, Any], *, line_number: int) -> TurnRecord:
    if record.get("type") != "turn":
        raise SessionCorruptError(f"unknown record type at line {line_number}")
    try:
        messages = ModelMessagesTypeAdapter.validate_python(record["messages"])
        plan_dict = record.get("plan")
        plan = PlanState.model_validate(plan_dict) if plan_dict is not None else PlanState()
        return TurnRecord(
            user_input=str(record["user_input"]),
            messages=messages,
            approvals=list(record["approvals"]),
            usage=dict(record["usage"]),
            status=str(record["status"]),
            created_at=str(record["created_at"]),
            plan=plan,
            diagnostics=list(record.get("diagnostics") or []),
            compaction=record.get("compaction"),
            error_message=(str(record["error_message"]) if record.get("error_message") is not None else None),
            partial_text=(str(record["partial_text"]) if record.get("partial_text") is not None else None),
            retryable=bool(record.get("retryable", False)),
            timeline_events=[
                TimelineEventRecord.from_dict(item) for item in list(record.get("timeline_events") or [])
            ],
            recovery_receipts=[dict(item) for item in list(record.get("recovery_receipts") or [])],
            channel=str(record.get("channel", "text")),
            interaction_id=(
                str(record["interaction_id"]) if record.get("interaction_id") is not None else None
            ),
            input_provenance=(
                str(record["input_provenance"]) if record.get("input_provenance") is not None else None
            ),
            provider_item_ids=[str(item) for item in list(record.get("provider_item_ids") or [])],
            live_metadata=dict(record.get("live_metadata") or {}),
            request_receipts=[
                ProviderRequestReceipt.model_validate(item)
                for item in list(record.get("request_receipts") or [])
            ],
            attachments=[
                AttachmentRef.model_validate(item)
                for item in list(record.get("attachments") or [])
            ],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SessionCorruptError(f"invalid turn at line {line_number}") from error


def _project_turn_history(
    turns: Sequence[TurnRecord],
) -> tuple[list[ModelMessage], list[ModelMessage], Any, Any, int, int]:
    """Rebuild the provider and compaction projections for one active lineage."""

    active_history: list[ModelMessage] = []
    full_history: list[ModelMessage] = []
    latest_summary: Any = None
    latest_checkpoint: Any = None
    compacted_prefix_length = 0
    compacted_source_end = 0
    for turn in turns:
        messages = turn.messages
        completed_steps = bool(messages) and any(
            item.get("kind") == "completed_model_steps"
            and item.get("message_count") == len(messages)
            for item in turn.diagnostics
        ) and not validate_active_history(messages)
        if turn.status not in {"completed", "waiting_for_user"} and not (
            turn.status in {"failed", "cancelled"} and completed_steps
        ):
            continue
        covered_messages = 0
        if turn.compaction is not None:
            candidate = _load_checkpoint(turn.compaction)
            raw_checkpoint: Any = turn.compaction.get("checkpoint")
            is_v2_record = (
                isinstance(raw_checkpoint, dict)
                and cast(dict[str, Any], raw_checkpoint).get("schema_version") == 2
            )
            if candidate is not None and not is_v2_record:
                candidate = _map_v1_checkpoint(candidate, full_history, latest_checkpoint)
            summary = (
                _summary_from_v2_checkpoint(candidate)
                if is_v2_record and candidate is not None
                else _load_summary(turn.compaction)
            )
            valid = not is_v2_record or (
                candidate is not None
                and len(full_history) <= candidate.source_end <= len(full_history) + len(messages)
                and _validate_v2_checkpoint(
                    candidate,
                    [*full_history, *messages][:candidate.source_end],
                    latest_checkpoint,
                )
            )
            if valid:
                if is_v2_record and candidate is not None:
                    covered_messages = candidate.source_end - len(full_history)
                active_history = _load_active_prefix(turn.compaction)
                compacted_prefix_length = len(active_history)
                latest_summary = summary
                latest_checkpoint = candidate
                compacted_source_end = (
                    candidate.source_end if candidate is not None else len(full_history)
                )
        full_history.extend(messages)
        active_history.extend(messages[covered_messages:])
    return (
        active_history,
        full_history,
        latest_summary,
        latest_checkpoint,
        compacted_prefix_length,
        compacted_source_end,
    )


def _load_active_prefix(compaction_raw: dict[str, Any]) -> list[ModelMessage]:
    """Materialise the compacted prefix from a stored compaction record."""

    active = compaction_raw.get("active_history")
    if active is None:
        return []
    try:
        return ModelMessagesTypeAdapter.validate_python(active)
    except ValueError:
        return []


def _load_summary(compaction_raw: dict[str, Any]) -> Any:
    """Materialise the ``ContextSummary`` from a stored compaction record.

    Imported lazily to avoid a circular import (context imports sessions
    indirectly via plan/types). Returns ``None`` when the summary is absent or
    fails validation, so callers treat it as "no prior summary".
    """

    summary_raw = compaction_raw.get("summary")
    if summary_raw is None:
        return None
    try:
        from lumen.context import ContextSummary

        return ContextSummary.model_validate(summary_raw)
    except Exception:
        return None


def _load_checkpoint(compaction_raw: dict[str, Any]) -> Any:
    raw = compaction_raw.get("checkpoint")
    if raw is None:
        return None
    try:
        from lumen.context.types import CompactionCheckpointV1, CompactionCheckpointV2

        if raw.get("schema_version") == 2:
            return CompactionCheckpointV2.model_validate(raw)
        return CompactionCheckpointV1.model_validate(raw)
    except Exception:
        return None


def _retrieval_terms(value: str) -> set[str]:
    words = {part.lower() for part in re.findall(r"[A-Za-z0-9_./:-]{3,}", value)}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", value))
    words.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {word for word in words if word}


def _validate_v2_checkpoint(
    checkpoint: Any,
    full_history: Sequence[ModelMessage],
    parent: Any,
) -> bool:
    """Validate V2 parent/range/cursor/digests against raw JSONL history."""

    from lumen.context.types import CompactionCheckpointV2

    if not isinstance(checkpoint, CompactionCheckpointV2):
        return False
    expected_start = parent.source_end if parent is not None else 0
    expected_parent = parent.checkpoint_id if parent is not None else None
    if checkpoint.parent_checkpoint_id != expected_parent or checkpoint.source_start != expected_start:
        return False
    if checkpoint.source_end != len(full_history):
        return False
    expected_start_id = (
        "session-origin"
        if checkpoint.source_start == 0
        else (
            parent.source_end_cursor.message_id
            if isinstance(parent, CompactionCheckpointV2)
            else f"legacy-boundary-{checkpoint.source_start}"
        )
    )
    if checkpoint.source_start_cursor.message_id != expected_start_id:
        return False
    source = list(full_history[checkpoint.source_start : checkpoint.source_end])
    payload = json.dumps(
        ModelMessagesTypeAdapter.dump_python(source, mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # Early V2 development builds emitted a 16-hex prefix. Continue to read
    # those records, but all new checkpoints persist the complete SHA-256.
    if checkpoint.source_digest not in {f"sha256:{digest}", f"sha256:{digest[:16]}"}:
        return False
    expected_end_id = "session-origin"
    if checkpoint.source_end:
        message_payload = json.dumps(
            ModelMessagesTypeAdapter.dump_python([full_history[checkpoint.source_end - 1]], mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        message_digest = hashlib.sha256(
            f"{checkpoint.source_end - 1}:".encode() + message_payload.encode()
        ).hexdigest()
        expected_end_id = f"msg-{message_digest[:16]}"
    if checkpoint.source_end_cursor.message_id != expected_end_id:
        return False
    state_digest = hashlib.sha256(checkpoint.rolling_state.model_dump_json().encode("utf-8")).hexdigest()
    return checkpoint.state_digest == f"sha256:{state_digest}"


def _summary_from_v2_checkpoint(checkpoint: Any) -> Any:
    from lumen.context import ContextSummary
    from lumen.context.types import CompactionCheckpointV2

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


def _map_v1_checkpoint(
    checkpoint: Any,
    full_history: Sequence[ModelMessage],
    parent: Any,
) -> Any:
    """Map legacy active-prefix counts onto absolute raw transcript ordinals."""

    from lumen.context.types import CompactionCheckpointV1, CompactionCheckpointV2

    if not isinstance(checkpoint, CompactionCheckpointV1) or isinstance(
        checkpoint,
        CompactionCheckpointV2,
    ):
        return checkpoint
    source_start = parent.source_end if parent is not None else 0
    return checkpoint.model_copy(
        update={
            "source_start": source_start,
            "source_end": len(full_history),
        }
    )
