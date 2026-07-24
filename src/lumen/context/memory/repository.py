"""Memory storage: protocol, in-memory and SQLite+FTS5 adapters (M5).

SQLite is the transactional authority (plan §11.2): versions, conflict status,
use statistics and FTS5 full-text search live there. The in-memory adapter is
the test double. Both implement :class:`MemoryRepository` so the manager and
projection never depend on SQLite.

A forgotten record is tombstoned: ``remember`` refuses to resurrect a forgotten
id, and ``/memory forget`` writes the tombstone so a replayed extraction cannot
revive the same fact (plan §11.4, §11.6). Conflict detection marks a new
record ``conflicted`` when it contradicts an active same-scope record rather
than silently overwriting it.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from lumen.context.memory.records import (
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryStatus,
)

#: Confidence floor for the default index (MEMORY_INDEX). Below this a record
#: is recallable by query but not loaded into every prompt's fixed prefix.
_INDEX_CONFIDENCE_FLOOR = 0.5


class MemoryRepository(Protocol):
    """Authoritative memory store (plan §11.2, §11.3)."""

    def remember(self, record: MemoryRecord) -> MemoryRecord: ...

    def forget(self, record_id: str) -> bool: ...

    def get(self, record_id: str) -> MemoryRecord | None: ...

    def list(
        self, scope: MemoryScope | None = None, *, include_forgotten: bool = False
    ) -> list[MemoryRecord]: ...

    def index(self) -> list[MemoryRecord]: ...

    def query(
        self,
        text: str,
        *,
        scope: MemoryScope | None = None,
        path: str | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]: ...

    def mark_used(self, record_id: str) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _is_active_for_index(record: MemoryRecord, now: datetime) -> bool:
    """Whether ``record`` belongs in the default MEMORY_INDEX (plan §11.5)."""

    if record.status is not MemoryStatus.ACTIVE:
        return False
    if record.confidence < _INDEX_CONFIDENCE_FLOOR:
        return False
    if record.valid_until is not None and record.valid_until < now:
        return False
    return True


def _rank(record: MemoryRecord, text: str) -> float:
    """A simple lexical-relevance rank for recall (plan §11.5 ordering sketch).

    Production replaces this with FTS5 ranking; the in-memory adapter uses it so
    recall ordering is deterministic in tests.
    """

    lowered = text.lower()
    content = record.content.lower()
    explicit = 1.0 if record.source_kind is MemorySource.EXPLICIT else 0.5
    relevance = sum(1.0 for token in lowered.split() if token and token in content)
    recency = 0.0  # last_used_at recency term; kept simple for the in-memory rank
    return explicit + relevance + recency + (record.use_count * 0.1)


class InMemoryMemoryRepository:
    """Process-local memory store for tests and ephemeral sessions.

    Conflict detection is conservative: two active records in the same scope
    whose content shares a topic phrase are both marked ``conflicted`` so
    neither enters the default index until the user resolves them.
    """

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._lock = threading.Lock()

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        with self._lock:
            existing = self._records.get(record.id)
            if existing is not None and existing.status is MemoryStatus.FORGOTTEN:
                # Tombstone: a forgotten fact must not be resurrected (plan §11.6).
                return existing
            stored = record
            if existing is not None and existing.status is MemoryStatus.ACTIVE:
                stored = record.model_copy(update={"supersedes": (*record.supersedes, existing.id)})
            self._records[record.id] = stored
            self._mark_conflicts(stored)
            return stored

    def forget(self, record_id: str) -> bool:
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return False
            self._records[record_id] = record.model_copy(update={"status": MemoryStatus.FORGOTTEN})
            return True

    def get(self, record_id: str) -> MemoryRecord | None:
        with self._lock:
            return self._records.get(record_id)

    def list(
        self, scope: MemoryScope | None = None, *, include_forgotten: bool = False
    ) -> list[MemoryRecord]:
        with self._lock:
            rows = list(self._records.values())
        result: list[MemoryRecord] = []
        for record in rows:
            if scope is not None and record.scope is not scope:
                continue
            if not include_forgotten and record.status is MemoryStatus.FORGOTTEN:
                continue
            result.append(record)
        return result

    def index(self) -> list[MemoryRecord]:
        now = _now()
        with self._lock:
            rows = list(self._records.values())
        return sorted(
            (r for r in rows if _is_active_for_index(r, now)),
            key=lambda r: (-r.confidence, r.created_at),
        )

    def query(
        self,
        text: str,
        *,
        scope: MemoryScope | None = None,
        path: str | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        now = _now()
        with self._lock:
            rows = list(self._records.values())
        candidates = [
            r
            for r in rows
            if _is_active_for_index(r, now)
            and (scope is None or r.scope is scope)
            and (path is None or r.path_glob is None or _glob_matches(r.path_glob, path))
        ]
        ranked = sorted(candidates, key=lambda r: _rank(r, text), reverse=True)
        return ranked[:limit]

    def mark_used(self, record_id: str) -> None:
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return
            self._records[record_id] = record.model_copy(
                update={
                    "last_used_at": _now(),
                    "use_count": record.use_count + 1,
                }
            )

    def _mark_conflicts(self, record: MemoryRecord) -> None:
        """Mark same-scope active records sharing a topic token as conflicted."""

        if record.status is not MemoryStatus.ACTIVE:
            return
        topic_tokens = {t.lower() for t in record.content.split() if len(t) > 3}
        if not topic_tokens:
            return
        for other in list(self._records.values()):
            if other.id == record.id or other.scope is not record.scope:
                continue
            if other.status is not MemoryStatus.ACTIVE:
                continue
            other_tokens = {t.lower() for t in other.content.split() if len(t) > 3}
            if topic_tokens & other_tokens:
                self._records[other.id] = other.model_copy(update={"status": MemoryStatus.CONFLICTED})
                self._records[record.id] = self._records[record.id].model_copy(
                    update={"status": MemoryStatus.CONFLICTED}
                )


def _glob_matches(glob: str, path: str) -> bool:
    """Shell-glob match for PATH-scope recall (plan §11.2)."""

    import fnmatch

    return fnmatch.fnmatch(path, glob)


class SQLiteMemoryRepository:
    """SQLite + FTS5 authoritative store (plan §11.2).

    The record body is stored as JSON in ``memory``; ``memory_fts`` is a FTS5
    virtual table over content for recall. The file is created with 0600 via
    the connection's ``mode`` pragma is not portable, so the caller ensures the
    directory is private; the repository itself never logs content.
    """

    schema = """
    CREATE TABLE IF NOT EXISTS memory (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        record_json TEXT NOT NULL,
        status TEXT NOT NULL,
        confidence REAL NOT NULL,
        path_glob TEXT,
        valid_until TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        id UNINDEXED, content, content='memory', content_rowid='rowid'
    );
    CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
        INSERT INTO memory_fts(id, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
        INSERT INTO memory_fts(memory_fts, id, content) VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
        INSERT INTO memory_fts(memory_fts, id, content) VALUES ('delete', old.id, old.content);
        INSERT INTO memory_fts(id, content) VALUES (new.id, new.content);
    END;
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        # RLock: remember/forget/mark_used call self.get while holding the lock.
        self._lock = threading.RLock()
        # Lazy: the SQLite file is created only on first use, so constructing a
        # repository (e.g. in tests that never write) touches nothing on disk.
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        """Connect and apply the schema on first use (lazy file creation)."""

        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(self.schema)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        with self._lock:
            existing = self.get(record.id)
            if existing is not None and existing.status is MemoryStatus.FORGOTTEN:
                return existing
            stored = record
            if existing is not None and existing.status is MemoryStatus.ACTIVE:
                stored = record.model_copy(update={"supersedes": (*record.supersedes, existing.id)})
            self._db().execute(
                "INSERT OR REPLACE INTO memory (id, scope, kind, content, record_json, status, "
                "confidence, path_glob, valid_until) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    stored.id,
                    stored.scope.value,
                    stored.kind.value,
                    stored.content,
                    stored.model_dump_json(),
                    stored.status.value,
                    stored.confidence,
                    stored.path_glob,
                    stored.valid_until.isoformat() if stored.valid_until else None,
                ),
            )
            self._db().commit()
            return stored

    def forget(self, record_id: str) -> bool:
        with self._lock:
            record = self.get(record_id)
            if record is None:
                return False
            forgotten = record.model_copy(update={"status": MemoryStatus.FORGOTTEN})
            self._db().execute(
                "UPDATE memory SET record_json=?, status=? WHERE id=?",
                (forgotten.model_dump_json(), MemoryStatus.FORGOTTEN.value, record_id),
            )
            self._db().commit()
            return True

    def get(self, record_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._db().execute("SELECT record_json FROM memory WHERE id=?", (record_id,)).fetchone()
        return MemoryRecord.model_validate_json(row["record_json"]) if row else None

    def list(
        self, scope: MemoryScope | None = None, *, include_forgotten: bool = False
    ) -> list[MemoryRecord]:
        with self._lock:
            if scope is None:
                rows = self._db().execute("SELECT record_json FROM memory").fetchall()
            else:
                rows = (
                    self._db()
                    .execute("SELECT record_json FROM memory WHERE scope=?", (scope.value,))
                    .fetchall()
                )
        records = [MemoryRecord.model_validate_json(r["record_json"]) for r in rows]
        if not include_forgotten:
            records = [r for r in records if r.status is not MemoryStatus.FORGOTTEN]
        return records

    def index(self) -> list[MemoryRecord]:
        now = _now()
        records = self.list()
        return sorted(
            (r for r in records if _is_active_for_index(r, now)),
            key=lambda r: (-r.confidence, r.created_at),
        )

    def query(
        self,
        text: str,
        *,
        scope: MemoryScope | None = None,
        path: str | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        now = _now()
        records = self.list(scope=scope)
        candidates = [
            r
            for r in records
            if _is_active_for_index(r, now)
            and (path is None or r.path_glob is None or _glob_matches(r.path_glob or "", path))
        ]
        # FTS5 match on content; fall back to all candidates when the query has
        # no FTS-safe tokens (e.g. CJK without tokenization).
        matching_ids: set[str] | None = None
        try:
            fts_rows = (
                self._db()
                .execute(
                    "SELECT id FROM memory_fts WHERE memory_fts MATCH ? LIMIT ?",
                    (_fts_query(text), limit * 4),
                )
                .fetchall()
            )
            matching_ids = {row["id"] for row in fts_rows}
        except sqlite3.OperationalError:
            matching_ids = None
        if matching_ids is not None:
            candidates = [r for r in candidates if r.id in matching_ids]
        ranked = sorted(candidates, key=lambda r: _rank(r, text), reverse=True)
        return ranked[:limit]

    def mark_used(self, record_id: str) -> None:
        with self._lock:
            record = self.get(record_id)
            if record is None:
                return
            used = record.model_copy(update={"last_used_at": _now(), "use_count": record.use_count + 1})
            self._db().execute(
                "UPDATE memory SET record_json=? WHERE id=?", (used.model_dump_json(), record_id)
            )
            self._db().commit()


def _fts_query(text: str) -> str:
    """Build an FTS5 MATCH query: each whitespace token as a prefix term."""

    tokens = [t for t in text.split() if t]
    if not tokens:
        return '""'
    return " ".join(f'"{t}"*' for t in tokens)


__all__ = [
    "InMemoryMemoryRepository",
    "MemoryRepository",
    "SQLiteMemoryRepository",
]
