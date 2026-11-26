"""Transactional authorization and projection ledger for the optional OpenViking index."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Collection, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lumen.context.memory.records import MemoryRecord, MemoryScope, MemorySource, MemoryStatus, Sensitivity
from lumen.context.memory.redaction import contains_secret

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ov_binding (
    id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, user TEXT NOT NULL,
    UNIQUE(endpoint, account, user)
);
CREATE TABLE IF NOT EXISTS ov_writer_key (
    endpoint TEXT NOT NULL, digest TEXT NOT NULL, binding TEXT NOT NULL, PRIMARY KEY(endpoint, digest)
);
CREATE TABLE IF NOT EXISTS ov_grant (
    binding TEXT NOT NULL, scope TEXT NOT NULL, enabled INTEGER NOT NULL, purge INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(binding, scope)
);
CREATE TABLE IF NOT EXISTS ov_object (
    id INTEGER PRIMARY KEY AUTOINCREMENT, binding TEXT NOT NULL, scope TEXT NOT NULL, record TEXT NOT NULL,
    uri TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, lease TEXT, lease_end REAL DEFAULT 0,
    created REAL NOT NULL, success REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS ov_pending ON ov_object(binding,scope,state,due);
CREATE INDEX IF NOT EXISTS ov_records ON ov_object(record,binding,state);
CREATE TABLE IF NOT EXISTS ov_private (record TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS ov_checkpoint (id INTEGER PRIMARY KEY CHECK (id=1), token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ov_backfill (
    binding TEXT NOT NULL, scope TEXT NOT NULL, record TEXT NOT NULL, source_row INTEGER NOT NULL,
    PRIMARY KEY(binding, scope, record)
);
CREATE TABLE IF NOT EXISTS ov_audit (
    binding TEXT PRIMARY KEY, next_audit REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ov_health (
    binding TEXT NOT NULL, scope TEXT NOT NULL, last_error TEXT, error_count INTEGER NOT NULL DEFAULT 0,
    backfill_since REAL, PRIMARY KEY(binding,scope)
);
CREATE TRIGGER IF NOT EXISTS ov_wait_start AFTER UPDATE OF state ON ov_object
WHEN OLD.state<>NEW.state AND NEW.state IN ('write','delete') BEGIN
    UPDATE ov_object SET created=(julianday('now')-2440587.5)*86400 WHERE id=NEW.id;
END;
"""

_AUDIT_PERIOD_SECONDS = 86_400
_AUDIT_SPACING_SECONDS = 30

_NEEDS_CONNECTION = """
    (g.enabled OR (g.purge AND EXISTS (
        SELECT 1 FROM ov_object o WHERE o.binding=g.binding AND o.scope=g.scope
        AND o.due<=:now AND o.lease_end<=:now AND (o.state='delete' OR (o.state='clean' AND NOT EXISTS (
            SELECT 1 FROM ov_audit a WHERE a.binding=o.binding AND a.next_audit>:now
        )))
    )))
"""


def _error_category(error: str) -> str:
    if error in {
        "authorization_failed",
        "invalid_response",
        "not_found",
        "delete_pending",
        "sync_timeout",
        "transport_error",
        "response_too_large",
        "invalid_identity",
        "identity_changed",
        "unsupported_service_identity",
    } or re.fullmatch(r"http_[1-5][0-9]{2}", error):
        return error
    return "failed"


@dataclass(frozen=True)
class VikingJob:
    id: int
    uri: str
    op: str
    lease: str
    attempts: int


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(project_id: str, user_scope: bool) -> str:
    return "user" if user_scope else f"projects/{_hash(project_id)}"


def _root(user: str, binding: str, scope: str) -> str:
    return f"viking://user/{user}/resources/lumen/{binding}/{scope}"


def _eligible(record: MemoryRecord) -> bool:
    now = datetime.now(UTC)
    return (
        record.status is MemoryStatus.ACTIVE
        and record.source_kind is MemorySource.EXPLICIT
        and record.scope in {MemoryScope.USER, MemoryScope.PROJECT}
        and record.sensitivity is not Sensitivity.RESTRICTED
        and record.valid_from <= now
        and (record.valid_until is None or record.valid_until > now)
        and not contains_secret(record.content)
    )


def _fingerprint(record: MemoryRecord) -> str:
    return _hash(
        record.model_dump_json(
            exclude={
                "created_at",
                "updated_at",
                "last_used_at",
                "use_count",
                "source_event_ids",
                "source_session_ids",
            }
        )
    )


class VikingStore:
    schema = _SCHEMA

    def __init__(self, db: Callable[[], sqlite3.Connection], lock: threading.RLock, path: Path) -> None:
        self._db = db
        self._lock = lock
        self._guard_path = path.with_name(path.name + ".openviking-guard")
        self._guard_connection: sqlite3.Connection | None = None
        self.private = False
        self._selector: tuple[str, str, str] | None = None

    def select_writer(self, endpoint: str, credential_hash: str, project_id: str) -> None:
        """Select a possible local outbox destination without opening the DB or granting network access."""
        if not re.fullmatch(r"[0-9a-f]{64}", credential_hash):
            raise ValueError("Invalid OpenViking credential fingerprint")
        with self._lock:
            self._selector = endpoint, credential_hash, project_id

    def lookup_writer(self, endpoint: str, credential_hash: str, project_id: str) -> str | None:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT k.binding FROM ov_writer_key k JOIN ov_grant g ON g.binding=k.binding "
                    "WHERE k.endpoint=? AND k.digest=? AND g.scope IN (?, 'user') LIMIT 1",
                    (endpoint, credential_hash, _scope(project_id, False)),
                )
                .fetchone()
            )
            return str(row[0]) if row else None

    def _verified_writer(
        self,
        db: sqlite3.Connection,
        endpoint: str,
        binding: str,
        project_id: str,
        credential_hash: str,
    ) -> None:
        self.select_writer(endpoint, credential_hash, project_id)
        db.execute("INSERT OR REPLACE INTO ov_writer_key VALUES(?,?,?)", (endpoint, credential_hash, binding))

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            db = self._db()
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def _guard(self, db: sqlite3.Connection) -> None:
        """Attach a separate local restore witness inside SQLite's multi-file transaction."""
        if self._guard_connection is db:
            return
        db.execute("ATTACH DATABASE ? AS ov_guard", (str(self._guard_path),))
        os.chmod(self._guard_path, 0o600)
        db.execute(
            "CREATE TABLE IF NOT EXISTS ov_guard.checkpoint "
            "(id INTEGER PRIMARY KEY CHECK (id=1), token TEXT NOT NULL)"
        )
        self._guard_connection = db

    def _checkpoint(self, db: sqlite3.Connection) -> None:
        self._guard(db)
        token = uuid.uuid4().hex
        db.execute("INSERT OR REPLACE INTO ov_checkpoint VALUES(1,?)", (token,))
        db.execute("INSERT OR REPLACE INTO ov_guard.checkpoint VALUES(1,?)", (token,))

    def check_restore(self, db: sqlite3.Connection) -> None:
        """Fail closed after restoring the main DB without its matching witness."""
        if not db.execute("SELECT 1 FROM ov_binding LIMIT 1").fetchone():
            return
        with db:
            db.execute("BEGIN IMMEDIATE")
            self._guard(db)
            matches = db.execute(
                "SELECT 1 FROM ov_checkpoint m JOIN ov_guard.checkpoint g ON g.token=m.token"
            ).fetchone()
            if not matches:
                db.execute("UPDATE ov_grant SET enabled=0,purge=0")
                db.execute("DELETE FROM ov_backfill")
                db.execute("UPDATE ov_object SET state='delete',lease=NULL,lease_end=0,due=0")
                self._checkpoint(db)

    def bind(
        self,
        endpoint: str,
        account: str,
        user: str,
        project_id: str,
        *,
        user_scope: bool = False,
        credential_hash: str,
    ) -> str:
        if self.private:
            raise ValueError("External memory is paused in privacy mode")
        if (
            not endpoint
            or not account
            or not project_id
            or not re.fullmatch(r"[\w.@-]+", user)
            or (user in {".", ".."})
        ):
            raise ValueError("Invalid OpenViking binding identity")
        scope = _scope(project_id, user_scope)
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO ov_binding VALUES(?,?,?,?)",
                (uuid.uuid4().hex, endpoint, account, user),
            )
            binding = str(
                db.execute(
                    "SELECT id FROM ov_binding WHERE endpoint=? AND account=? AND user=?",
                    (endpoint, account, user),
                ).fetchone()[0]
            )
            db.execute(
                "INSERT INTO ov_grant VALUES(?,?,1,0) ON CONFLICT(binding,scope) DO UPDATE SET "
                "enabled=1,purge=0",
                (binding, scope),
            )
            self._verified_writer(db, endpoint, binding, project_id, credential_hash)
            self._checkpoint(db)
        return binding

    def lookup(self, endpoint: str, account: str, user: str, project_id: str) -> str | None:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT id FROM ov_binding JOIN ov_grant ON binding=id WHERE endpoint=? AND account=? "
                    "AND user=? AND scope IN (?, 'user') LIMIT 1",
                    (endpoint, account, user, _scope(project_id, False)),
                )
                .fetchone()
            )
        return str(row[0]) if row else None

    def connect(
        self, endpoint: str, account: str, user: str, project_id: str, *, credential_hash: str
    ) -> str | None:
        with self.transaction() as db:
            binding = self.lookup(endpoint, account, user, project_id)
            if binding:
                self._verified_writer(db, endpoint, binding, project_id, credential_hash)
            else:
                self._selector = None
        return binding

    def grants(self, binding: str, project_id: str) -> tuple[bool, bool]:
        with self._lock:
            scopes = {
                r[0]
                for r in self._db().execute(
                    "SELECT scope FROM ov_grant WHERE binding=? AND enabled", (binding,)
                )
            }
        return _scope(project_id, False) in scopes, "user" in scopes

    def has_work(self, binding: str, project_id: str) -> bool:
        with self._lock:
            return (
                self._db()
                .execute(
                    "SELECT 1 FROM ov_grant g WHERE g.binding=:binding AND g.scope IN (:scope, 'user') "
                    f"AND {_NEEDS_CONNECTION} LIMIT 1",
                    {"binding": binding, "scope": _scope(project_id, False), "now": time.time()},
                )
                .fetchone()
                is not None
            )

    def eligible_count(self, project_id: str, *, user_scope: bool = False) -> int:
        if self.private:
            return 0
        with self._lock:
            rows = self._db().execute(
                "SELECT record_json FROM memory WHERE scope=? AND status='active' AND "
                "(scope='user' OR json_extract(record_json,'$.project_id')=?) "
                "AND id NOT IN (SELECT record FROM ov_private)",
                ("user" if user_scope else "project", project_id),
            )
            return sum(_eligible(MemoryRecord.model_validate_json(row[0])) for row in rows)

    def disable(
        self, binding: str, project_id: str, *, user_scope: bool = False, purge: bool = False
    ) -> None:
        scope = _scope(project_id, user_scope)
        with self.transaction() as db:
            db.execute(
                "UPDATE ov_grant SET enabled=0,purge=? WHERE binding=? AND scope=?",
                (int(purge), binding, scope),
            )
            db.execute("DELETE FROM ov_backfill WHERE binding=? AND scope=?", (binding, scope))
            if purge:
                db.execute(
                    "UPDATE ov_object SET state='delete',due=0 WHERE binding=? AND scope=?", (binding, scope)
                )
            self._checkpoint(db)

    def changed(self, db: sqlite3.Connection, record: MemoryRecord) -> None:
        """Called inside every authority mutation, including offline or private writers."""
        db.execute("DELETE FROM ov_backfill WHERE record=?", (record.id,))
        if self.private:
            db.execute("INSERT OR IGNORE INTO ov_private VALUES(?)", (record.id,))
        else:
            db.execute("DELETE FROM ov_private WHERE record=?", (record.id,))
        if not db.execute("SELECT 1 FROM ov_binding LIMIT 1").fetchone():
            return
        digest = _fingerprint(record)
        valid = _eligible(record) and not self.private
        db.execute(
            "UPDATE ov_object SET state='delete',due=0 WHERE record=? AND state IN ('write','ready') "
            "AND (digest<>? OR ?=0)",
            (record.id, digest, int(valid)),
        )
        if valid and self._selector:
            endpoint, credential_hash, project = self._selector
            if record.scope is MemoryScope.USER or record.project_id == project:
                binding = self.lookup_writer(endpoint, credential_hash, project)
                if binding:
                    scope = _scope(project, record.scope is MemoryScope.USER)
                    self._enqueue(db, binding, scope, record, digest)
        self._checkpoint(db)

    @staticmethod
    def _enqueue(db: sqlite3.Connection, binding: str, scope: str, record: MemoryRecord, digest: str) -> None:
        grant = db.execute(
            "SELECT user FROM ov_binding JOIN ov_grant ON binding=id WHERE id=? AND scope=? AND enabled",
            (binding, scope),
        ).fetchone()
        if (
            grant is None
            or db.execute(
                "SELECT 1 FROM ov_object WHERE binding=? AND scope=? AND record=? "
                "AND digest=? AND state IN ('write','ready')",
                (binding, scope, record.id, digest),
            ).fetchone()
        ):
            return
        # Appending an immutable version makes late writes harmless to the current version.
        cur = db.execute(
            "INSERT INTO ov_object(binding,scope,record,uri,digest,state,created) "
            "VALUES(?,?,?,'',?,'write',?)",
            (binding, scope, record.id, digest, time.time()),
        )
        # The nonce avoids URI reuse even after restoring an older sqlite_sequence.
        version = f"{cur.lastrowid}-{uuid.uuid4().hex}"
        uri = f"{_root(str(grant[0]), binding, scope)}/{_hash(record.id)}/{version}.md"
        db.execute("UPDATE ov_object SET uri=? WHERE id=?", (uri, cur.lastrowid))

    def backfill(self, binding: str, project_id: str, *, user_scope: bool = False) -> None:
        if self.private:
            raise ValueError("Backfill is paused in privacy mode")
        scope = _scope(project_id, user_scope)
        with self.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM ov_grant WHERE binding=? AND scope=? AND enabled", (binding, scope)
            ).fetchone():
                raise ValueError("The requested OpenViking scope is not enabled")
            # Capture identifiers only. A worker visits bounded pages; later mutations cancel
            # the snapshot entry and handle the new version under current authorization.
            if not db.execute(
                "SELECT 1 FROM ov_backfill WHERE binding=? AND scope=? LIMIT 1", (binding, scope)
            ).fetchone():
                db.execute(
                    "INSERT INTO ov_health(binding,scope,backfill_since) VALUES(?,?,?) "
                    "ON CONFLICT(binding,scope) DO UPDATE SET backfill_since=excluded.backfill_since",
                    (binding, scope, time.time()),
                )
            db.execute(
                "INSERT OR IGNORE INTO ov_backfill SELECT ?,?,m.id,m.rowid FROM memory m "
                "WHERE m.scope=? AND m.status='active' AND (scope='user' OR "
                "json_extract(record_json,'$.project_id')=?) AND "
                "json_extract(record_json,'$.source_kind')='explicit' AND "
                "json_extract(record_json,'$.sensitivity')<>'restricted' AND "
                "m.id NOT IN (SELECT record FROM ov_private) AND NOT EXISTS (SELECT 1 FROM ov_object o "
                "WHERE o.binding=? AND o.scope=? AND o.record=m.id AND o.state IN ('ready','write'))",
                (binding, scope, "user" if user_scope else "project", project_id, binding, scope),
            )
            # A remotely restored index may have lost previously acknowledged objects. Recheck
            # each known URI on explicit backfill, retaining its immutable revision.
            db.execute(
                "UPDATE ov_object SET state='write',due=0 WHERE binding=? AND scope=? AND state='ready'",
                (binding, scope),
            )
            self._checkpoint(db)

    def _backfill_page(self, db: sqlite3.Connection, binding: str, project_id: str) -> None:
        rows = db.execute(
            "SELECT q.scope,q.record,m.record_json FROM ov_backfill q JOIN ov_grant g USING(binding,scope) "
            "LEFT JOIN memory m ON m.id=q.record AND m.rowid=q.source_row WHERE q.binding=? AND "
            "q.scope IN (?, 'user') AND g.enabled ORDER BY q.record LIMIT 64",
            (binding, _scope(project_id, False)),
        ).fetchall()
        for scope, record_id, data in rows:
            if data:
                record = MemoryRecord.model_validate_json(data)
                if _eligible(record) and (
                    record.scope is MemoryScope.USER or record.project_id == project_id
                ):
                    self._enqueue(db, binding, scope, record, _fingerprint(record))
            db.execute(
                "DELETE FROM ov_backfill WHERE binding=? AND scope=? AND record=?",
                (binding, scope, record_id),
            )

    def claim(self, binding: str, project_id: str) -> VikingJob | None:
        if self.private:
            return None
        now = time.time()
        scopes = binding, _scope(project_id, False)
        with self.transaction() as db:
            self._backfill_page(db, binding, project_id)
            # Expiry happens without a memory write. A restored or externally changed record
            # also needs validation at dispatch and recall time.
            db.execute(
                "UPDATE ov_object SET state='delete',due=0 WHERE binding=? AND scope IN (?, 'user') "
                "AND state IN ('ready','write') AND record IN (SELECT id FROM memory WHERE "
                "valid_until IS NOT NULL AND julianday(valid_until)<=julianday('now'))",
                scopes,
            )
            row = db.execute(
                "SELECT o.* FROM ov_object o JOIN ov_grant g USING(binding,scope) WHERE binding=? "
                "AND scope IN (?, 'user') AND (g.enabled OR g.purge) AND (state IN ('write','delete') OR "
                "(state IN ('ready','clean') AND NOT EXISTS (SELECT 1 FROM ov_audit a "
                "WHERE a.binding=o.binding AND a.next_audit>?))) AND due<=? AND lease_end<=? "
                "ORDER BY (state='delete') DESC,(state='write') DESC,due,id LIMIT 1",
                (*scopes, now, now, now),
            ).fetchone()
            if not row:
                return None
            if row["state"] in {"ready", "clean"}:
                db.execute(
                    "INSERT OR REPLACE INTO ov_audit VALUES(?,?)", (binding, now + _AUDIT_SPACING_SECONDS)
                )
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE ov_object SET lease=?,lease_end=?,attempts=attempts+1 WHERE id=?",
                (token, now + 60, row["id"]),
            )
            return VikingJob(
                row["id"],
                row["uri"],
                {"ready": "verify", "write": "write", "delete": "delete", "clean": "delete"}[row["state"]],
                token,
                row["attempts"] + 1,
            )

    @staticmethod
    def _record(db: sqlite3.Connection, row: sqlite3.Row) -> MemoryRecord | None:
        record_row = db.execute(
            "SELECT record_json FROM memory WHERE id=? AND NOT EXISTS "
            "(SELECT 1 FROM ov_private WHERE record=?)",
            (row["record"], row["record"]),
        ).fetchone()
        if record_row:
            record = MemoryRecord.model_validate_json(record_row[0])
            if _eligible(record) and _fingerprint(record) == row["digest"]:
                return record
        return None

    def validate(self, job: VikingJob) -> str | None:
        if self.private:
            return None
        with self.transaction() as db:
            row = db.execute(
                "SELECT o.*,g.enabled,g.purge FROM ov_object o JOIN ov_grant g USING(binding,scope) "
                "WHERE id=? AND lease=? AND lease_end>?",
                (job.id, job.lease, time.time()),
            ).fetchone()
            if not row:
                return None
            if (
                job.op == "delete"
                and row["state"] in {"delete", "clean"}
                and (row["enabled"] or row["purge"])
            ):
                return ""
            if row["enabled"] and (job.op, row["state"]) in {("write", "write"), ("verify", "ready")}:
                record = self._record(db, row)
                if record:
                    return record.content if job.op == "write" else ""
                db.execute("UPDATE ov_object SET state='delete',due=0 WHERE id=?", (job.id,))
            return None

    def finish(self, job: VikingJob, *, error: str | None = None, missing: bool = False) -> None:
        now = time.time()
        with self.transaction() as db:
            row = db.execute(
                "SELECT o.*,g.enabled FROM ov_object o JOIN ov_grant g USING(binding,scope) "
                "WHERE id=? AND lease=?",
                (job.id, job.lease),
            ).fetchone()
            if not row:
                # An abandoned writer can return even after another worker removed its URI.
                if job.op == "write":
                    db.execute(
                        "UPDATE ov_object SET state='delete',due=0 WHERE id=? AND state='clean'", (job.id,)
                    )
                return
            state = row["state"]
            due = 0.0
            success = None
            if error:
                due = now + 2 ** min(8, job.attempts)
            elif job.op == "write" and state == "write":
                state = "ready"
                due = now + _AUDIT_PERIOD_SECONDS
                success = now
            elif job.op == "verify" and state == "ready":
                due = now + _AUDIT_PERIOD_SECONDS
                if missing and row["enabled"] and not self.private:
                    state = "write" if self._record(db, row) else "delete"
                    due = now
                success = now
            elif job.op == "delete" and state in {"delete", "clean"}:
                state = "clean"
                # Verify older deletions again after the write lease horizon and after restore.
                due = now + (60 if row["state"] == "delete" else _AUDIT_PERIOD_SECONDS)
                success = now
            category = _error_category(error) if error else None
            if error or success is not None:
                db.execute(
                    "INSERT INTO ov_health(binding,scope,last_error,error_count) VALUES(?,?,?,?) "
                    "ON CONFLICT(binding,scope) DO UPDATE SET last_error=excluded.last_error,"
                    "error_count=CASE WHEN excluded.last_error IS NULL THEN 0 ELSE error_count+1 END",
                    (row["binding"], row["scope"], category, int(error is not None)),
                )
            db.execute(
                "UPDATE ov_object SET state=?,due=?,lease=NULL,lease_end=0,error=?,"
                "success=coalesce(?,success) WHERE id=?",
                (state, due, category, success, job.id),
            )

    def roots(self, binding: str, project_id: str, exclude_ids: Collection[str]) -> list[str]:
        if self.private:
            return []
        with self._lock:
            db = self._db()
            rows = db.execute(
                "SELECT b.user,o.* FROM ov_object o JOIN ov_binding b ON o.binding=b.id "
                "JOIN ov_grant g ON g.binding=o.binding AND g.scope=o.scope WHERE o.binding=? AND "
                "o.scope IN (?, 'user') AND g.enabled AND o.state='ready'",
                (binding, _scope(project_id, False)),
            )
            roots: dict[str, str] = {}
            for row in rows:
                scope = str(row["scope"])
                if scope not in roots and row["record"] not in exclude_ids and self._record(db, row):
                    roots[scope] = _root(str(row["user"]), binding, scope)
            return sorted(roots.values())

    def resolve(self, binding: str, project_id: str, uris: Sequence[str]) -> list[MemoryRecord]:
        if self.private:
            return []
        with self._lock:
            db = self._db()
            hits: list[MemoryRecord] = []
            for uri in dict.fromkeys(uris):
                row = db.execute(
                    "SELECT o.* FROM ov_object o JOIN ov_grant g USING(binding,scope) WHERE binding=? "
                    "AND scope IN (?, 'user') AND enabled AND state='ready' AND uri=?",
                    (binding, _scope(project_id, False), uri),
                ).fetchone()
                if row and (record := self._record(db, row)):
                    hits.append(record)
            return hits

    def status(self, binding: str, project_id: str, *, user_scope: bool = False) -> dict[str, object]:
        selected = (binding, _scope(project_id, user_scope))
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT count(CASE WHEN state='write' THEN 1 END),"
                    "count(CASE WHEN state='delete' THEN 1 END),"
                    "count(CASE WHEN state='ready' THEN 1 END),max(success),"
                    "min(CASE WHEN state IN ('write','delete') THEN created END) FROM ov_object "
                    "WHERE binding=? AND scope=?",
                    selected,
                )
                .fetchone()
            )
            project, user = self.grants(binding, project_id)
            health = (
                self._db()
                .execute(
                    "SELECT last_error,error_count,backfill_since FROM ov_health WHERE binding=? AND scope=?",
                    selected,
                )
                .fetchone()
            )
            backfill = int(
                self._db()
                .execute("SELECT count(*) FROM ov_backfill WHERE binding=? AND scope=?", selected)
                .fetchone()[0]
            )
            starts = [
                float(value)
                for value in (row[4], health[2] if health and backfill else None)
                if value is not None
            ]
        return {
            "enabled": user if user_scope else project,
            "project_enabled": project,
            "user_enabled": user,
            "pending": row[0] + backfill,
            "backfill": backfill,
            "cleanup": row[1],
            "ready": row[2],
            "last_success": row[3],
            "last_error": health[0] if health else None,
            "consecutive_errors": health[1] if health else 0,
            "oldest_pending_seconds": max(0.0, time.time() - min(starts)) if starts else 0.0,
        }
