from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from lumen.context.session_state import SessionContextState
from lumen.events import TimelineEventRecord
from lumen.plan import PlanState

SCHEMA_VERSION = 5


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


@dataclass(frozen=True, slots=True)
class TurnPage:
    """A chronological page of turns and the cursor for the preceding page."""

    turns: list[TurnRecord]
    next_before: int | None


@dataclass(frozen=True, slots=True)
class SessionData:
    metadata: SessionMetadata
    turns: list[TurnRecord]
    """Active model history reconstructed for the next run.

    ``history`` reflects what the model should see: it includes the
    compaction summary prefix plus the recent complete turns produced since the
    most recent compaction. ``full_history`` is the append-only record of every
    raw completed turn and is what gets persisted across compactions.
    """
    history: list[ModelMessage]
    full_history: list[ModelMessage]
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


class SessionRepository:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

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
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
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
        }
        if compaction is not None:
            record["compaction"] = self._dump_compaction(compaction)
        if error_message is not None:
            record["error_message"] = error_message
        if partial_text is not None:
            record["partial_text"] = partial_text
        if retryable:
            record["retryable"] = True
        self._append(path, record)

    def append_context_state(self, session_id: str, state: SessionContextState) -> None:
        """Append a v5 source-state snapshot without rewriting prior records."""

        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        self._append(
            path,
            {
                "type": "context_state",
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
        with path.open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, 1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "turn":
                    continue
                try:
                    turn = _parse_turn(record, line_number=line_number)
                except SessionCorruptError:
                    continue
                if turn.status not in {"completed", "waiting_for_user"}:
                    continue
                compaction = turn.compaction
                if compaction is not None:
                    checkpoint = _load_checkpoint(compaction)
                    summary = _load_summary(compaction)
                    raw_checkpoint = compaction.get("checkpoint")
                    is_v2 = (
                        isinstance(raw_checkpoint, dict)
                        and raw_checkpoint.get("schema_version") == 2
                    )
                    if checkpoint is not None and not is_v2:
                        checkpoint = _map_v1_checkpoint(checkpoint, full_history, parent)
                    if is_v2 and checkpoint is not None:
                        summary = _summary_from_v2_checkpoint(checkpoint)
                    valid = checkpoint is not None and summary is not None and (
                        not is_v2
                        or _validate_v2_checkpoint(
                            checkpoint,
                            full_history,
                            parent,
                        )
                    )
                    if valid:
                        checkpoint_id = str(checkpoint.checkpoint_id)
                        episodes.append(
                            (
                                line_number,
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
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, 1):
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
        if schema_version not in {1, 2, 3, 4, 5}:
            raise SessionCorruptError(f"unsupported session schema: {schema_version}")
        metadata = SessionMetadata(
            id=str(header["id"]),
            agent_name=str(header["agent_name"]),
            model_id=str(header["model_id"]),
            created_at=str(header["created_at"]),
            path=path,
        )
        turns: list[TurnRecord] = []
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
        for line_number, record in enumerate(records[1:], 2):
            if record.get("type") == "context_state":
                if schema_version < 5:
                    raise SessionCorruptError(
                        f"context_state is not valid for schema v{schema_version} at line {line_number}"
                    )
                try:
                    context_state = SessionContextState.model_validate(record.get("state"))
                except ValueError as error:
                    raise SessionCorruptError(f"invalid context_state at line {line_number}") from error
                continue
            turn = _parse_turn(record, line_number=line_number)
            messages = turn.messages
            turns.append(turn)
            if turn.status in {"completed", "waiting_for_user"}:
                if turn.compaction is not None:
                    candidate = _load_checkpoint(turn.compaction)
                    raw_checkpoint = turn.compaction.get("checkpoint")
                    is_v2_record = (
                        isinstance(raw_checkpoint, dict)
                        and raw_checkpoint.get("schema_version") == 2
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
                        and _validate_v2_checkpoint(
                            candidate,
                            full_history,
                            latest_compaction_checkpoint,
                        )
                    )
                    if valid:
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
                active_history.extend(messages)
            # Model history only accepts completed turns, but the latest plan
            # is diagnostic state and remains useful after failure/cancel.
            latest_plan = turn.plan
        return SessionData(
            metadata,
            turns,
            active_history,
            full_history,
            latest_plan,
            latest_compaction_summary,
            latest_compaction_checkpoint,
            compacted_prefix_length,
            compacted_source_end,
            context_state,
        )

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
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")

        selected: deque[tuple[dict[str, Any], int]] = deque(maxlen=limit)
        turn_count = 0
        with path.open(encoding="utf-8") as file:
            header_line = file.readline()
            if not header_line:
                raise SessionCorruptError("missing session header")
            header = _parse_json_record(header_line, line_number=1, path=path)
            if header.get("type") != "session":
                raise SessionCorruptError("missing session header")
            schema_version = header.get("schema_version")
            if schema_version not in {1, 2, 3, 4, 5}:
                raise SessionCorruptError(f"unsupported session schema: {schema_version}")

            for line_number, line in enumerate(file, 2):
                record = _parse_json_record(line, line_number=line_number, path=path)
                if record.get("type") == "context_state":
                    if schema_version < 5:
                        raise SessionCorruptError(
                            f"context_state is not valid for schema v{schema_version} at line {line_number}"
                        )
                    continue
                if record.get("type") != "turn":
                    raise SessionCorruptError(f"unknown record type at line {line_number}")
                if before is None or turn_count < before:
                    selected.append((record, line_number))
                turn_count += 1

        end = turn_count if before is None else before
        if not 0 <= end <= turn_count:
            raise ValueError(f"invalid turn page cursor: {before}")
        start = max(0, end - limit)
        turns = [_parse_turn(record, line_number=line) for record, line in selected]
        return TurnPage(turns=turns, next_before=start if start > 0 else None)

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
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SessionCorruptError(f"invalid turn at line {line_number}") from error


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
    expected_start_id = "session-origin" if checkpoint.source_start == 0 else (
        parent.source_end_cursor.message_id
        if isinstance(parent, CompactionCheckpointV2)
        else f"legacy-boundary-{checkpoint.source_start}"
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
