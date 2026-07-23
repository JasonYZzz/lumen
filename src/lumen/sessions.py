from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from lumen.events import TimelineEventRecord
from lumen.plan import PlanState

SCHEMA_VERSION = 4


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

    @staticmethod
    def _dump_compaction(compaction: Any) -> dict[str, Any]:
        """Persist only the compacted prefix and the summary, not the source."""

        summary = getattr(compaction, "summary", None)
        active_history = getattr(compaction, "active_history", None)
        source_message_count = getattr(compaction, "source_message_count", 0)
        usage = getattr(compaction, "usage", {}) or {}
        return {
            "summary": summary.model_dump(mode="json") if summary is not None else None,
            "active_history": ModelMessagesTypeAdapter.dump_python(list(active_history or []), mode="json"),
            "source_message_count": int(source_message_count),
            "usage": dict(usage),
        }

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
        if schema_version not in {1, 2, 3, 4}:
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
        for line_number, record in enumerate(records[1:], 2):
            turn = _parse_turn(record, line_number=line_number)
            messages = turn.messages
            turns.append(turn)
            if turn.status == "completed":
                full_history.extend(messages)
                if turn.compaction is not None:
                    # Reset the active window from the stored prefix; later
                    # successful turns append to it.
                    active_history = _load_active_prefix(turn.compaction)
                    # Track the latest summary so /resume can restore it for
                    # iterative compaction, isolated to this session.
                    latest_compaction_summary = _load_summary(turn.compaction)
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
            if schema_version not in {1, 2, 3, 4}:
                raise SessionCorruptError(f"unsupported session schema: {schema_version}")

            for line_number, line in enumerate(file, 2):
                record = _parse_json_record(line, line_number=line_number, path=path)
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


def _parse_json_record(line: str, *, line_number: int, path: Path) -> dict[str, Any]:
    try:
        raw_record: Any = json.loads(line)
    except json.JSONDecodeError as error:
        raise SessionCorruptError(f"invalid JSON at line {line_number} in {path.name}") from error
    if not isinstance(raw_record, dict):
        raise SessionCorruptError(f"record at line {line_number} is not an object")
    return cast(dict[str, Any], raw_record)


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
