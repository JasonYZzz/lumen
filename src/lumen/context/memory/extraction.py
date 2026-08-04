"""Recoverable stage-one memory extraction and work queue (M6)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from lumen.context.memory.records import MemoryKind, MemoryScope, MemorySource
from lumen.context.memory.redaction import contains_secret

DEFAULT_MIN_SESSION_TURNS = 4


@dataclass(frozen=True, slots=True)
class ExtractionProvenance:
    """Host-computed source policy; the extraction model cannot alter it."""

    user_event_ids: frozenset[str] = frozenset()
    external_event_ids: frozenset[str] = frozenset()
    locally_verified_event_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SessionExtractionSource:
    """A session snapshot loaded from the append-only session repository."""

    session_id: str
    transcript_text: str
    turn_count: int
    has_stable_result: bool
    incognito: bool = False
    provenance: ExtractionProvenance = field(default_factory=ExtractionProvenance)

    @property
    def digest(self) -> str:
        return transcript_digest(self.transcript_text)


class RawFact(BaseModel):
    """A model-proposed fact. Source trust is resolved outside this object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    scope: MemoryScope = MemoryScope.PROJECT
    kind: MemoryKind = MemoryKind.PROJECT_FACT
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionCandidate:
    """A provenance-checked, sensitivity-screened fact ready for consolidation."""

    content: str
    scope: MemoryScope
    kind: MemoryKind
    confidence: float
    source_kind: MemorySource
    source_session_id: str
    source_event_ids: tuple[str, ...] = ()
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionJob:
    id: str
    session_id: str
    transcript_digest: str


class MemoryWorkQueue(Protocol):
    """Durable outbox plus cross-process per-scope leases."""

    def enqueue(self, session_id: str, digest: str) -> ExtractionJob | None: ...

    def claim(self, *, lease_seconds: float) -> ExtractionJob | None: ...

    def complete(self, job_id: str) -> None: ...

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        base_delay_seconds: float,
        max_attempts: int,
    ) -> None: ...

    def cancel_pending(self, session_id: str | None = None) -> None: ...

    def acquire_scope(self, scope: MemoryScope, owner: str, *, lease_seconds: float) -> bool: ...

    def release_scope(self, scope: MemoryScope, owner: str) -> None: ...

    def stats(self) -> dict[str, int]: ...


class InMemoryMemoryWorkQueue:
    """Deterministic test adapter with the same idempotency semantics."""

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[ExtractionJob, str, int, float]] = {}
        self._keys: set[tuple[str, str]] = set()
        self._leases: dict[MemoryScope, tuple[str, float]] = {}
        self._lock = threading.RLock()

    def enqueue(self, session_id: str, digest: str) -> ExtractionJob | None:
        with self._lock:
            key = (session_id, digest)
            if key in self._keys:
                return None
            for job_id, (job, status, attempts, next_at) in list(self._jobs.items()):
                if job.session_id == session_id and status in {"queued", "processing"}:
                    self._jobs[job_id] = (job, "superseded", attempts, next_at)
            job = ExtractionJob(f"job-{uuid4().hex}", session_id, digest)
            self._jobs[job.id] = (job, "queued", 0, 0.0)
            self._keys.add(key)
            return job

    def claim(self, *, lease_seconds: float) -> ExtractionJob | None:
        del lease_seconds
        now = time.time()
        with self._lock:
            for job_id, (job, status, attempts, next_at) in self._jobs.items():
                if status == "queued" and next_at <= now:
                    self._jobs[job_id] = (job, "processing", attempts, next_at)
                    return job
        return None

    def complete(self, job_id: str) -> None:
        with self._lock:
            job, _status, attempts, next_at = self._jobs[job_id]
            self._jobs[job_id] = (job, "completed", attempts, next_at)

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        base_delay_seconds: float,
        max_attempts: int,
    ) -> None:
        del error
        with self._lock:
            job, _status, attempts, _next_at = self._jobs[job_id]
            attempts += 1
            status = "dead" if attempts >= max_attempts else "queued"
            delay = base_delay_seconds * (2 ** max(0, attempts - 1))
            self._jobs[job_id] = (job, status, attempts, time.time() + delay)

    def cancel_pending(self, session_id: str | None = None) -> None:
        with self._lock:
            for job_id, (job, status, attempts, next_at) in list(self._jobs.items()):
                if status not in {"queued", "processing"}:
                    continue
                if session_id is not None and job.session_id != session_id:
                    continue
                self._jobs[job_id] = (job, "cancelled", attempts, next_at)

    def acquire_scope(self, scope: MemoryScope, owner: str, *, lease_seconds: float) -> bool:
        now = time.time()
        with self._lock:
            current = self._leases.get(scope)
            if current is not None and current[0] != owner and current[1] > now:
                return False
            self._leases[scope] = (owner, now + lease_seconds)
            return True

    def release_scope(self, scope: MemoryScope, owner: str) -> None:
        with self._lock:
            if self._leases.get(scope, (None, 0.0))[0] == owner:
                self._leases.pop(scope, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            result: dict[str, int] = {}
            for _job, status, _attempts, _next_at in self._jobs.values():
                result[status] = result.get(status, 0) + 1
            return result


class SQLiteMemoryWorkQueue:
    """SQLite outbox with crash-reclaimable jobs and cross-process leases."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS extraction_jobs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        transcript_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at REAL NOT NULL,
        UNIQUE(session_id, transcript_digest)
    );
    CREATE INDEX IF NOT EXISTS extraction_jobs_ready
        ON extraction_jobs(status, next_attempt, created_at);
    CREATE TABLE IF NOT EXISTS memory_scope_leases (
        scope TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        lease_until REAL NOT NULL
    );
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()
            os.chmod(self._path, 0o600)
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def enqueue(self, session_id: str, digest: str) -> ExtractionJob | None:
        now = time.time()
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT id FROM extraction_jobs WHERE session_id=? AND transcript_digest=?",
                    (session_id, digest),
                ).fetchone()
                if existing is not None:
                    db.commit()
                    return None
                db.execute(
                    "UPDATE extraction_jobs SET status='superseded' "
                    "WHERE session_id=? AND status IN ('queued','processing')",
                    (session_id,),
                )
                job = ExtractionJob(f"job-{uuid4().hex}", session_id, digest)
                db.execute(
                    "INSERT INTO extraction_jobs "
                    "(id,session_id,transcript_digest,status,created_at) VALUES (?,?,?,?,?)",
                    (job.id, job.session_id, job.transcript_digest, "queued", now),
                )
                db.commit()
                return job
            except BaseException:
                db.rollback()
                raise

    def claim(self, *, lease_seconds: float) -> ExtractionJob | None:
        now = time.time()
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT id,session_id,transcript_digest FROM extraction_jobs "
                    "WHERE (status='queued' AND next_attempt<=?) "
                    "OR (status='processing' AND lease_until<=?) "
                    "ORDER BY created_at LIMIT 1",
                    (now, now),
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                db.execute(
                    "UPDATE extraction_jobs SET status='processing', lease_until=? WHERE id=?",
                    (now + lease_seconds, row["id"]),
                )
                db.commit()
                return ExtractionJob(row["id"], row["session_id"], row["transcript_digest"])
            except BaseException:
                db.rollback()
                raise

    def complete(self, job_id: str) -> None:
        with self._lock:
            db = self._db()
            db.execute(
                "UPDATE extraction_jobs SET status='completed', lease_until=0, last_error=NULL WHERE id=?",
                (job_id,),
            )
            db.commit()

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        base_delay_seconds: float,
        max_attempts: int,
    ) -> None:
        with self._lock:
            db = self._db()
            row = db.execute("SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            status = "dead" if attempts >= max_attempts else "queued"
            next_attempt = time.time() + base_delay_seconds * (2 ** max(0, attempts - 1))
            db.execute(
                "UPDATE extraction_jobs SET status=?, attempts=?, next_attempt=?, "
                "lease_until=0, last_error=? WHERE id=?",
                (status, attempts, next_attempt, error[:1000], job_id),
            )
            db.commit()

    def cancel_pending(self, session_id: str | None = None) -> None:
        with self._lock:
            db = self._db()
            if session_id is None:
                db.execute(
                    "UPDATE extraction_jobs SET status='cancelled', lease_until=0 "
                    "WHERE status IN ('queued','processing')"
                )
            else:
                db.execute(
                    "UPDATE extraction_jobs SET status='cancelled', lease_until=0 "
                    "WHERE session_id=? AND status IN ('queued','processing')",
                    (session_id,),
                )
            db.commit()

    def acquire_scope(self, scope: MemoryScope, owner: str, *, lease_seconds: float) -> bool:
        now = time.time()
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT owner,lease_until FROM memory_scope_leases WHERE scope=?",
                    (scope.value,),
                ).fetchone()
                if row is not None and row["owner"] != owner and float(row["lease_until"]) > now:
                    db.commit()
                    return False
                db.execute(
                    "INSERT INTO memory_scope_leases(scope,owner,lease_until) VALUES (?,?,?) "
                    "ON CONFLICT(scope) DO UPDATE SET owner=excluded.owner, lease_until=excluded.lease_until",
                    (scope.value, owner, now + lease_seconds),
                )
                db.commit()
                return True
            except BaseException:
                db.rollback()
                raise

    def release_scope(self, scope: MemoryScope, owner: str) -> None:
        with self._lock:
            db = self._db()
            db.execute(
                "DELETE FROM memory_scope_leases WHERE scope=? AND owner=?",
                (scope.value, owner),
            )
            db.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = (
                self._db()
                .execute("SELECT status,COUNT(*) AS count FROM extraction_jobs GROUP BY status")
                .fetchall()
            )
        return {str(row["status"]): int(row["count"]) for row in rows}


def transcript_digest(transcript_text: str) -> str:
    return hashlib.sha256(transcript_text.encode()).hexdigest()


def is_eligible_session(
    *,
    turn_count: int,
    incognito: bool,
    has_stable_result: bool,
    min_turns: int = DEFAULT_MIN_SESSION_TURNS,
) -> bool:
    return not incognito and turn_count >= min_turns and has_stable_result


def extract_candidates(
    facts: list[RawFact],
    *,
    session_id: str,
    provenance: ExtractionProvenance,
    project_id: str | None = None,
) -> list[ExtractionCandidate]:
    """Apply host provenance, external exclusion and sensitivity rejection."""

    candidates: list[ExtractionCandidate] = []
    seen: set[str] = set()
    for fact in facts:
        source_ids = frozenset(fact.source_event_ids)
        # Auto-learned facts require traceable evidence. This also prevents an
        # extraction model from inventing an id or laundering external content
        # through an assistant paraphrase.
        if not source_ids:
            continue
        eligible_source_ids = (
            provenance.user_event_ids | provenance.external_event_ids | provenance.locally_verified_event_ids
        )
        if not source_ids <= eligible_source_ids:
            continue
        external_only = source_ids <= provenance.external_event_ids
        verified = bool(source_ids & provenance.locally_verified_event_ids)
        if external_only and not verified:
            continue
        if contains_secret(fact.content):
            continue
        content = fact.content.strip()
        key = content.casefold()
        if not content or key in seen:
            continue
        seen.add(key)
        candidates.append(
            ExtractionCandidate(
                content=content,
                scope=fact.scope,
                kind=fact.kind,
                confidence=fact.confidence,
                source_kind=MemorySource.OBSERVED if verified else MemorySource.INFERRED,
                source_session_id=session_id,
                source_event_ids=fact.source_event_ids,
                project_id=None if fact.scope is MemoryScope.USER else project_id,
            )
        )
    return candidates


__all__ = [
    "DEFAULT_MIN_SESSION_TURNS",
    "ExtractionCandidate",
    "ExtractionJob",
    "ExtractionProvenance",
    "InMemoryMemoryWorkQueue",
    "MemoryWorkQueue",
    "RawFact",
    "SQLiteMemoryWorkQueue",
    "SessionExtractionSource",
    "extract_candidates",
    "is_eligible_session",
    "transcript_digest",
]
