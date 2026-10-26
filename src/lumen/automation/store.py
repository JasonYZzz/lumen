"""One transactional authority for definitions, occurrences and assistant user state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .models import (
    AssistantEdit,
    AssistantState,
    AutomationCommand,
    Collection,
    ContextRef,
    Definition,
    FeedItem,
    Goal,
    GoalCommand,
    GoalDetail,
    GoalEvidence,
    GoalExecution,
    GoalExecutionStatus,
    Idea,
    Occurrence,
    Phase,
    Resource,
    Result,
    resource_file_type,
)

T = TypeVar("T", bound=BaseModel)

# Keep tombstones for recovery, but never offer feed actions for unavailable targets.
_VISIBLE_FEED = """
    (json_extract(feed.body,'$.resource_id') IS NULL OR EXISTS (
        SELECT 1 FROM objects AS target WHERE target.kind='resource'
        AND target.id=json_extract(feed.body,'$.resource_id')
        AND json_extract(target.body,'$.deleted_at') IS NULL
    )) AND (json_extract(feed.body,'$.goal_id') IS NULL OR EXISTS (
        SELECT 1 FROM objects AS target WHERE target.kind='goal'
        AND target.id=json_extract(feed.body,'$.goal_id')
        AND json_extract(target.body,'$.deleted_at') IS NULL
    ))
"""

# Goal attention is actionable only for the activation and run that produced it.
_CURRENT_GOAL_ATTENTION = """
    (feed.id NOT LIKE 'goal-attention:%' OR EXISTS (
        SELECT 1 FROM objects AS target WHERE target.kind='goal'
        AND target.id=json_extract(feed.body,'$.goal_id')
        AND json_extract(target.body,'$.status')='active'
        AND json_extract(target.body,'$.execution.status') IN ('review','blocked','budget_reached')
        AND feed.id='goal-attention:' || target.id || ':'
            || json_extract(target.body,'$.execution.id') || ':'
            || COALESCE(json_extract(target.body,'$.execution.run_id'),'pending')
    ))
"""

if TYPE_CHECKING:
    from lumen.work_products.types import WorkProductRef


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class AssistantConflict(ValueError):
    pass


class AutomationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._connection = sqlite3.connect(self.path, timeout=3, check_same_thread=False)
            try:
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1, 2}:
                    raise ValueError("Unsupported automation database version")
                tables = {
                    row[0]
                    for row in self._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
                    )
                }
                expected = {"objects", "requests", "editions", "checkpoints", "owned_sessions", "feed"}
                extended = {
                    "resource_versions",
                    "goal_resources",
                    "collection_resources",
                    "resource_activity",
                    "assistant_runs",
                }
                if (version == 0 and tables) or (
                    version > 0 and tables != (expected | extended if version == 2 else expected)
                ):
                    raise ValueError("Assistant database layout is unavailable; refusing to recreate state")
                if version == 1:
                    backup_path = self.path.with_name(f"{self.path.name}.v1-{uuid4().hex}.bak")
                    with sqlite3.connect(backup_path) as backup:
                        self._connection.backup(backup)
                    backup_path.chmod(0o600)
                self.path.chmod(0o600)
                self._connection.execute("PRAGMA foreign_keys=ON")
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=FULL")
                self._connection.executescript("""
                    CREATE TABLE IF NOT EXISTS objects (
                        kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                        PRIMARY KEY(kind,id));
                    CREATE TABLE IF NOT EXISTS requests (
                        id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS editions (
                        automation_id TEXT NOT NULL, edition TEXT NOT NULL,
                        PRIMARY KEY(automation_id,edition));
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        automation_id TEXT NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL,
                        PRIMARY KEY(automation_id,path));
                    CREATE TABLE IF NOT EXISTS owned_sessions (id TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS feed (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
                """)
                self._connection.commit()
                if version < 2:
                    with self.transaction() as db:
                        db.execute(
                            "CREATE TABLE resource_versions (id TEXT, revision INTEGER, body TEXT NOT NULL, "
                            "PRIMARY KEY(id,revision))"
                        )
                        db.execute(
                            "CREATE TABLE goal_resources "
                            "(goal_id TEXT, resource_id TEXT, role TEXT NOT NULL, "
                            "PRIMARY KEY(goal_id,resource_id))"
                        )
                        db.execute(
                            "CREATE TABLE collection_resources (collection_id TEXT, resource_id TEXT, "
                            "PRIMARY KEY(collection_id,resource_id))"
                        )
                        db.execute(
                            "CREATE TABLE resource_activity (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "id TEXT NOT NULL UNIQUE)"
                        )
                        db.execute(
                            "CREATE TABLE assistant_runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                            "request_id TEXT NOT NULL, body TEXT NOT NULL, "
                            "UNIQUE(session_id,request_id))"
                        )
                        rows = db.execute(
                            "SELECT body FROM objects WHERE kind='resource' "
                            "ORDER BY json_extract(body,'$.created_at'),id"
                        ).fetchall()
                        for row in rows:
                            resource = Resource.model_validate_json(row[0])
                            resource = resource.model_copy(
                                update={
                                    "published": resource.saved or resource.kind in {"report", "note"},
                                }
                            )
                            self._put(db, "resource", resource, resource.id)
                        db.execute("PRAGMA user_version=2")
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("Assistant store is closed")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _put(self, db: sqlite3.Connection, kind: str, value: BaseModel, identity: str) -> None:
        if kind == "resource" and isinstance(value, Resource):
            previous = db.execute(
                "SELECT body FROM objects WHERE kind='resource' AND id=?", (identity,)
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO resource_versions VALUES(?,?,?)",
                (identity, value.revision, value.model_dump_json()),
            )
            if (
                previous is None
                or Resource.model_validate_json(previous[0]).artifact_ref != value.artifact_ref
            ):
                db.execute("DELETE FROM resource_activity WHERE id=?", (identity,))
                db.execute("INSERT INTO resource_activity(id) VALUES(?)", (identity,))
            else:
                db.execute("INSERT OR IGNORE INTO resource_activity(id) VALUES(?)", (identity,))
        db.execute(
            "INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
            (kind, identity, value.model_dump_json()),
        )

    def _get(
        self, db: sqlite3.Connection, kind: str, identity: str, model: type[T],
        *, include_deleted: bool = False,
    ) -> T:
        row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {kind}: {identity}")
        value = model.model_validate_json(row[0])
        if isinstance(value, Goal | Resource) and value.deleted_at and not include_deleted:
            raise ValueError("This item is in the recycle bin; restore it before using it")
        return value

    def get(self, kind: str, identity: str, model: type[T]) -> T:
        with self.transaction() as db:
            return self._get(db, kind, identity, model)

    def list(self, kind: str, model: type[T], *, limit: int = 200) -> tuple[T, ...]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT body FROM objects WHERE kind=? ORDER BY rowid DESC LIMIT ?", (kind, limit)
            ).fetchall()
            return tuple(model.model_validate_json(row[0]) for row in rows)

    def command(self, command: AutomationCommand, fingerprint: str, now: datetime) -> str:
        with self.transaction() as db:
            identity = f"automation:{command.request_id}"
            payload_digest = digest(command.model_dump(mode="json"))
            prior = db.execute("SELECT digest,result FROM requests WHERE id=?", (identity,)).fetchone()
            if prior is not None:
                if prior[0] != payload_digest:
                    raise AssistantConflict("Request identity was already used with another payload")
                return str(prior[1])
            if command.action == "create":
                if command.spec is None:
                    raise ValueError("An automation specification is required")
                definition = Definition(
                    id=str(uuid4()),
                    revision=1,
                    spec=command.spec,
                    created_at=now,
                    updated_at=now,
                )
                self._put(db, "definition", definition, definition.id)
                result = definition.id
                if command.goal_id is not None:
                    goal = self._get(db, "goal", command.goal_id, Goal)
                    self._put(
                        db,
                        "goal",
                        goal.model_copy(
                            update={
                                "automation_ids": (*goal.automation_ids, definition.id),
                                "revision": goal.revision + 1,
                            }
                        ),
                        goal.id,
                    )
            else:
                if command.id is None or command.expected_revision is None:
                    raise ValueError("id and expected_revision are required")
                definition = self._get(db, "definition", command.id, Definition)
                if definition.revision != command.expected_revision:
                    raise AssistantConflict("Automation changed; reload before applying your decision")
                result = definition.id
                if command.action in {"run", "trial"}:
                    if command.action == "run" and definition.validation != fingerprint:
                        raise AssistantConflict("Run a successful trial with the current configuration first")
                    occurrence = self._new_occurrence(
                        definition,
                        "trial" if command.action == "trial" else "manual",
                        f"manual:{command.request_id}",
                        now,
                    )
                    self._put(db, "occurrence", occurrence, occurrence.id)
                    result = occurrence.id
                else:
                    values: dict[str, Any] = {"updated_at": now, "revision": definition.revision + 1}
                    if command.action == "update":
                        if command.spec is None:
                            raise ValueError("An automation specification is required")
                        values.update(spec=command.spec, enabled=False, validation=None, next_due=None)
                        values["revision"] = definition.revision + 1
                        if command.spec.model_dump(exclude={"name", "notify"}) == definition.spec.model_dump(
                            exclude={"name", "notify"}
                        ):
                            values.update(
                                enabled=definition.enabled,
                                validation=fingerprint if definition.validation else None,
                                next_due=definition.next_due,
                            )
                        else:
                            db.execute("DELETE FROM checkpoints WHERE automation_id=?", (definition.id,))
                    elif command.action == "enable":
                        if definition.validation != fingerprint:
                            raise AssistantConflict(
                                "Run a successful trial with the current configuration first"
                            )
                        if definition.spec.end_at is not None and definition.spec.end_at <= now:
                            raise ValueError("Automation has already reached its end time")
                        values.update(enabled=True, next_due=definition.spec.schedule.next_after(now))
                    else:
                        values.update(enabled=False, next_due=None)
                    self._put(db, "definition", definition.model_copy(update=values), definition.id)
                    control_only = command.action == "enable" or (
                        command.action == "update" and values.get("validation") is not None
                    )
                    if control_only:
                        for row in db.execute("SELECT body FROM objects WHERE kind='occurrence'").fetchall():
                            pending = Occurrence.model_validate_json(row[0])
                            if pending.automation_id == definition.id and pending.phase is Phase.QUEUED:
                                self._put(
                                    db,
                                    "occurrence",
                                    pending.model_copy(
                                        update={
                                            "definition_revision": definition.revision + 1,
                                        }
                                    ),
                                    pending.id,
                                )
                    if command.action in {"update", "pause"} and not control_only:
                        rows = db.execute("SELECT body FROM objects WHERE kind='occurrence'").fetchall()
                        for row in rows:
                            pending = Occurrence.model_validate_json(row[0])
                            if pending.automation_id == definition.id and pending.phase is Phase.QUEUED:
                                self._put(
                                    db,
                                    "occurrence",
                                    pending.model_copy(
                                        update={
                                            "phase": Phase.SETTLED,
                                            "result": Result.SKIPPED,
                                            "detail": "Definition changed or paused before admission",
                                        }
                                    ),
                                    pending.id,
                                )
            db.execute("INSERT INTO requests VALUES(?,?,?)", (identity, payload_digest, result))
            return result

    def _new_occurrence(
        self,
        definition: Definition,
        kind: str,
        edition: str,
        now: datetime,
    ) -> Occurrence:
        return Occurrence.model_validate(
            {
                "id": str(uuid4()),
                "automation_id": definition.id,
                "definition_revision": definition.revision,
                "kind": kind,
                "edition": edition,
                "created_at": now,
                "session_id": str(uuid4()),
                "interaction_id": str(uuid4()),
            }
        )

    def materialize(self, now: datetime) -> None:
        with self.transaction() as db:
            for row in db.execute("SELECT body FROM objects WHERE kind='definition'").fetchall():
                definition = Definition.model_validate_json(row[0])
                if not definition.enabled or definition.next_due is None or definition.next_due > now:
                    continue
                if definition.spec.end_at is not None and definition.spec.end_at <= now:
                    self._put(
                        db,
                        "definition",
                        definition.model_copy(
                            update={
                                "enabled": False,
                                "next_due": None,
                                "updated_at": now,
                            }
                        ),
                        definition.id,
                    )
                    continue
                due = definition.spec.schedule.latest_due(now)
                local = due.astimezone(ZoneInfo(definition.spec.schedule.timezone))
                edition = local.date().isoformat()
                inserted = db.execute(
                    "INSERT OR IGNORE INTO editions VALUES(?,?)",
                    (definition.id, edition),
                ).rowcount
                if inserted:
                    occurrence = self._new_occurrence(definition, "scheduled", edition, now)
                    if due > definition.next_due:
                        occurrence = occurrence.model_copy(
                            update={
                                "detail": f"Missed editions coalesced from {definition.next_due.isoformat()}",
                            }
                        )
                    self._put(db, "occurrence", occurrence, occurrence.id)
                self._put(
                    db,
                    "definition",
                    definition.model_copy(
                        update={
                            "next_due": definition.spec.schedule.next_after(now),
                        }
                    ),
                    definition.id,
                )

    def pending(self) -> tuple[Occurrence, ...]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT body FROM objects WHERE kind='occurrence' "
                "AND json_extract(body,'$.phase') IN ('queued','running','settling') "
                "ORDER BY CASE json_extract(body,'$.phase') WHEN 'queued' THEN 1 ELSE 0 END, rowid LIMIT 100"
            ).fetchall()
            return tuple(Occurrence.model_validate_json(row[0]) for row in rows)

    def put_occurrence(self, occurrence: Occurrence) -> None:
        with self.transaction() as db:
            current = self._get(db, "occurrence", occurrence.id, Occurrence)
            if current.phase is Phase.SETTLED:
                return
            self._put(db, "occurrence", occurrence, occurrence.id)

    def begin(
        self,
        occurrence: Occurrence,
        fingerprint: str,
        now: datetime,
        preferences: tuple[str, ...] = (),
    ) -> Occurrence:
        with self.transaction() as db:
            occurrence = self._get(db, "occurrence", occurrence.id, Occurrence)
            definition = self._get(db, "definition", occurrence.automation_id, Definition)
            if definition.revision != occurrence.definition_revision:
                raise AssistantConflict("Definition was superseded")
            if occurrence.kind == "scheduled" and not definition.enabled:
                raise AssistantConflict("Automation was paused")
            if occurrence.kind != "trial" and definition.validation != fingerprint:
                raise AssistantConflict("Configuration changed; a new trial is required")
            running = occurrence.model_copy(
                update={
                    "phase": Phase.RUNNING,
                    "fingerprint": fingerprint,
                    "deadline": now + timedelta(seconds=definition.spec.timeout_seconds),
                    "spec_snapshot": definition.spec,
                    "preference_snapshot": preferences,
                }
            )
            self._put(db, "occurrence", running, running.id)
            return running

    def settle(self, occurrence: Occurrence, text: str = "") -> None:
        with self.transaction() as db:
            current = self._get(db, "occurrence", occurrence.id, Occurrence)
            if current.phase is Phase.SETTLED:
                return
            definition = self._get(db, "definition", occurrence.automation_id, Definition)
            successful = occurrence.result in {Result.COMPLETE, Result.NO_CHANGE, Result.PARTIAL}
            goal_ids = [
                goal.id
                for row in db.execute(
                    "SELECT body FROM objects WHERE kind='goal' AND json_extract(body,'$.deleted_at') IS NULL"
                )
                if definition.id in (goal := Goal.model_validate_json(row[0])).automation_ids
            ]
            if occurrence.artifact_ref and occurrence.result in {Result.COMPLETE, Result.PARTIAL}:
                resource = Resource(
                    id=occurrence.id,
                    title=(
                        occurrence.spec_snapshot.name if occurrence.spec_snapshot else definition.spec.name
                    ),
                    kind="trial" if occurrence.kind == "trial" else "report",
                    artifact_ref=occurrence.artifact_ref,
                    session_id=occurrence.session_id,
                    created_at=occurrence.created_at,
                    published=occurrence.kind != "trial",
                    interaction_id=occurrence.interaction_id,
                )
                self._put(db, "resource", resource, resource.id)
                db.execute(
                    "INSERT INTO objects VALUES('resource_search',?,?) "
                    "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                    (resource.id, json.dumps({"text": text})),
                )
                if occurrence.kind != "trial":
                    for goal_id in goal_ids:
                        db.execute(
                            "INSERT OR REPLACE INTO goal_resources VALUES(?,?,?)",
                            (goal_id, resource.id, "output"),
                        )
            if occurrence.kind == "trial":
                if (
                    occurrence.result is Result.COMPLETE
                    and definition.revision == occurrence.definition_revision
                ):
                    self._put(
                        db,
                        "definition",
                        definition.model_copy(
                            update={
                                "validation": occurrence.fingerprint,
                            }
                        ),
                        definition.id,
                    )
            elif successful and (
                definition.revision == occurrence.definition_revision
                or definition.spec == occurrence.spec_snapshot
            ):
                for receipt in occurrence.receipts:
                    if receipt.error is None and receipt.revision is not None:
                        db.execute(
                            "INSERT INTO checkpoints VALUES(?,?,?) "
                            "ON CONFLICT(automation_id,path) DO UPDATE SET revision=excluded.revision",
                            (definition.id, receipt.path, receipt.revision),
                        )
            if occurrence.kind != "trial" and occurrence.result is not Result.NO_CHANGE:
                item = FeedItem(
                    sequence=0,
                    id=occurrence.id,
                    title=occurrence.spec_snapshot.name if occurrence.spec_snapshot else definition.spec.name,
                    result=occurrence.result or Result.FAILED,
                    detail=occurrence.detail,
                    created_at=occurrence.created_at,
                    resource_id=occurrence.id if occurrence.artifact_ref and successful else None,
                    occurrence_id=occurrence.id,
                    goal_id=goal_ids[0] if goal_ids else None,
                    resource_revision=1 if occurrence.artifact_ref and successful else None,
                    read=not definition.spec.notify and occurrence.result is Result.COMPLETE,
                )
                db.execute(
                    "INSERT OR IGNORE INTO feed(id,body) VALUES(?,?)", (item.id, item.model_dump_json())
                )
            if occurrence.result in {Result.NEEDS_USER_ACTION, Result.INTERRUPTED} and (
                definition.revision == occurrence.definition_revision
                or definition.spec == occurrence.spec_snapshot
            ):
                self._put(
                    db,
                    "definition",
                    definition.model_copy(
                        update={
                            "enabled": False,
                            "validation": None,
                            "next_due": None,
                        }
                    ),
                    definition.id,
                )
            self._put(db, "occurrence", occurrence.model_copy(update={"phase": Phase.SETTLED}), occurrence.id)

    def feed(self, *, before: int | None = None, limit: int = 100) -> tuple[FeedItem, ...]:
        with self.transaction() as db:
            rows = db.execute(
                f"SELECT sequence,body FROM feed WHERE sequence < ? AND {_VISIBLE_FEED} "
                "ORDER BY sequence DESC LIMIT ?",
                (before if before is not None else 2**63 - 1, limit),
            ).fetchall()
            return tuple(
                FeedItem.model_validate_json(row[1]).model_copy(update={"sequence": row[0]}) for row in rows
            )

    def page(
        self,
        kind: str,
        before: int | None,
        query: str,
        category: str,
        limit: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as db:
            params: list[str | int] = [before or 2**63 - 1]
            if kind == "feed":
                if category not in {"all", "attention", "unread"}:
                    raise ValueError("Unknown feed category")
                table, cursor = "feed", "sequence"
                clauses = ["sequence < ?", _VISIBLE_FEED]
                if category == "attention":
                    clauses.append("json_extract(body,'$.result') NOT IN ('complete','no_change','skipped')")
                    clauses.append(_CURRENT_GOAL_ATTENTION)
                elif category == "unread":
                    clauses.append("json_extract(body,'$.read')=0")
            elif kind == "resource":
                if category not in {
                    "all",
                    "saved",
                    "report",
                    "note",
                    "trial",
                    "work_product",
                    "materials",
                    "hidden",
                    "trash",
                    "document", "site", "image", "spreadsheet", "presentation", "code", "file",
                } and not category.startswith("collection:"):
                    raise ValueError("Unknown resource category")
                table, cursor = (
                    "objects JOIN resource_activity ON objects.id=resource_activity.id",
                    "sequence",
                )
                clauses = ["kind='resource'", "sequence < ?"]
                clauses.append("json_extract(body,'$.deleted_at') IS NOT NULL" if category == "trash"
                               else "json_extract(body,'$.deleted_at') IS NULL")
                if category not in {"materials", "hidden", "work_product", "trial", "trash"}:
                    clauses.append("json_extract(body,'$.published')=1")
                elif category in {"materials", "hidden"}:
                    clauses.append("json_extract(body,'$.published')=0")
                if category == "saved":
                    clauses.append("json_extract(body,'$.saved')=1")
                elif category.startswith("collection:"):
                    clauses.append(
                        "objects.id IN (SELECT resource_id FROM collection_resources WHERE collection_id=?)"
                    )
                    params.append(category.removeprefix("collection:"))
                elif category in {"document", "site", "image", "spreadsheet", "presentation", "code", "file"}:
                    clauses.append("json_extract(body,'$.file_type')=?")
                    params.append(category)
                elif category not in {"all", "materials", "hidden", "trash"}:
                    clauses.append("json_extract(body,'$.kind')=?")
                    params.append(category)
                if session_id is not None:
                    clauses.append("json_extract(body,'$.session_id')=?")
                    params.append(session_id)
            else:
                raise ValueError("Unknown assistant projection")
            if query:
                clauses.append(
                    "(instr(lower(json_extract(body,'$.title')),lower(?))>0 "
                    "OR instr(lower(coalesce((SELECT json_extract(search.body,'$.text') "
                    "FROM objects AS search "
                    "WHERE search.kind='resource_search' AND search.id=objects.id),'')),lower(?))>0)"
                    if kind == "resource"
                    else "instr(lower(json_extract(body,'$.title')),lower(?))>0"
                )
                params.extend([query, query] if kind == "resource" else [query])
            params.append(limit + 1)
            rows = db.execute(
                f"SELECT {cursor},body FROM {table} WHERE {' AND '.join(clauses)} "
                f"ORDER BY {cursor} DESC LIMIT ?",
                params,
            ).fetchall()
            items = [json.loads(row[1]) for row in rows[:limit]]
            if kind == "feed":
                for item, row in zip(items, rows[:limit], strict=True):
                    item["sequence"] = row[0]
                    if item.get("resource_id"):
                        resource = db.execute(
                            "SELECT body FROM objects WHERE kind='resource' AND id=?",
                            (item["resource_id"],),
                        ).fetchone()
                        item["saved"] = bool(resource and Resource.model_validate_json(resource[0]).saved)
            return {"items": items, "next_cursor": rows[limit - 1][0] if len(rows) > limit else None}

    def edit(self, edit: AssistantEdit, *, artifact_ref: str | None = None) -> None:
        with self.transaction() as db:
            if edit.action in {"trash_goal", "restore_goal", "trash_resource", "restore_resource"}:
                is_goal = edit.action.endswith("goal")
                kind = "goal" if is_goal else "resource"
                item = self._get(db, kind, edit.id, Goal if is_goal else Resource, include_deleted=True)
                deleting = edit.action.startswith("trash_")
                if bool(item.deleted_at) == deleting:
                    return
                if item.revision != edit.expected_revision:
                    raise AssistantConflict("Item changed; reload before applying your decision")
                if deleting and isinstance(item, Goal):
                    for identity in item.automation_ids:
                        definition = self._get(db, "definition", identity, Definition)
                        if definition.enabled or db.execute(
                            "SELECT 1 FROM objects WHERE kind='occurrence' "
                            "AND json_extract(body,'$.automation_id')=? "
                            "AND json_extract(body,'$.phase') IN ('queued','running','settling')",
                            (identity,),
                        ).fetchone():
                            raise AssistantConflict(
                                "Pause linked follow-ups and wait for execution before deleting"
                            )
                self._put(db, kind, item.model_copy(update={
                    "deleted_at": datetime.now(UTC) if deleting else None, "revision": item.revision + 1,
                }), item.id)
            elif edit.action == "rename_resource":
                resource = self._get(db, "resource", edit.id, Resource)
                title = edit.title.strip()
                if not title:
                    raise ValueError("A resource needs a title")
                if resource.title == title:
                    return
                if resource.revision != edit.expected_revision:
                    raise AssistantConflict("Resource changed; reload before renaming")
                self._put(db, "resource", resource.model_copy(update={
                    "title": title, "revision": resource.revision + 1, "updated_at": datetime.now(UTC),
                }), resource.id)
            elif edit.action == "delete_collection":
                row = db.execute(
                    "SELECT body FROM objects WHERE kind='collection' AND id=?", (edit.id,),
                ).fetchone()
                if row is None:
                    return
                collection = Collection.model_validate_json(row[0])
                if collection.revision != edit.expected_revision:
                    raise AssistantConflict("Collection changed; reload before deleting")
                db.execute("DELETE FROM collection_resources WHERE collection_id=?", (edit.id,))
                db.execute("DELETE FROM objects WHERE kind='collection' AND id=?", (edit.id,))
            elif edit.action == "save":
                resource = self._get(db, "resource", edit.id, Resource)
                self._put(
                    db,
                    "resource",
                    resource.model_copy(
                        update={
                            "saved": edit.value,
                            "published": resource.published or edit.value,
                        }
                    ),
                    resource.id,
                )
            elif edit.action == "visibility":
                resource = self._get(db, "resource", edit.id, Resource)
                self._put(db, "resource", resource.model_copy(update={"published": edit.value}), resource.id)
            elif edit.action == "link_resource":
                self._get(db, "resource", edit.id, Resource)
                if edit.goal_id is None:
                    raise ValueError("Select a goal")
                self._get(db, "goal", edit.goal_id, Goal)
                if edit.value:
                    db.execute(
                        "INSERT OR REPLACE INTO goal_resources VALUES(?,?,?)",
                        (edit.goal_id, edit.id, edit.role),
                    )
                else:
                    db.execute(
                        "DELETE FROM goal_resources WHERE goal_id=? AND resource_id=?",
                        (edit.goal_id, edit.id),
                    )
            elif edit.action == "collection":
                if not edit.title.strip():
                    raise ValueError("A collection needs a title")
                row = db.execute(
                    "SELECT body FROM objects WHERE kind='collection' AND id=?", (edit.id,)
                ).fetchone()
                current = Collection.model_validate_json(row[0]) if row else None
                if current and current.title == edit.title:
                    return
                if current and current.revision != edit.expected_revision:
                    raise AssistantConflict("Collection changed; reload before editing")
                self._put(
                    db,
                    "collection",
                    Collection(id=edit.id, title=edit.title, revision=current.revision + 1 if current else 1),
                    edit.id,
                )
            elif edit.action == "collect":
                self._get(db, "resource", edit.id, Resource)
                self._get(db, "collection", edit.collection_id or "", Collection)
                if edit.value:
                    db.execute(
                        "INSERT OR IGNORE INTO collection_resources VALUES(?,?)",
                        (edit.collection_id, edit.id),
                    )
                else:
                    db.execute(
                        "DELETE FROM collection_resources WHERE collection_id=? AND resource_id=?",
                        (edit.collection_id, edit.id),
                    )
            elif edit.action == "read":
                row = db.execute("SELECT body FROM feed WHERE id=?", (edit.id,)).fetchone()
                if row is None:
                    raise ValueError("Unknown feed item")
                item = FeedItem.model_validate_json(row[0]).model_copy(update={"read": edit.value})
                db.execute("UPDATE feed SET body=? WHERE id=?", (item.model_dump_json(), item.id))
            elif edit.action == "goal":
                if edit.goal is None:
                    raise ValueError("Goal content is required")
                row = db.execute("SELECT body FROM objects WHERE kind='goal' AND id=?", (edit.id,)).fetchone()
                if row and Goal.model_validate_json(row[0]).deleted_at:
                    raise ValueError("Restore this goal before editing")
                revision = Goal.model_validate_json(row[0]).revision if row else 0
                if row and Goal.model_validate_json(row[0]).model_dump(
                    exclude={"id", "revision", "primary_session_id", "deleted_at", "execution"}
                ) == (edit.goal.model_dump()):
                    return
                if revision and revision != edit.expected_revision:
                    raise AssistantConflict("Goal changed; reload before editing")
                for identity in edit.goal.automation_ids:
                    self._get(db, "definition", identity, Definition)
                existing = Goal.model_validate_json(row[0]) if row else None
                execution = existing.execution if existing else None
                if existing and execution and execution.status is GoalExecutionStatus.ACTIVE and (
                    edit.goal.status != "active" or edit.goal.title != existing.title
                    or edit.goal.intent != existing.intent
                ):
                    raise AssistantConflict("请先暂停目标推进。再修改目标范围或收尾")
                closing = existing and existing.status != edit.goal.status and edit.goal.status != "active"
                definitions = tuple(self._get(db, "definition", item, Definition)
                                    for item in edit.goal.automation_ids)
                if closing and any(d.enabled for d in definitions) and edit.follow_up_policy is None:
                    raise AssistantConflict("目标仍有启用的持续跟进。请明确选择暂停或保留")
                if closing and edit.follow_up_policy == "pause":
                    for definition in definitions:
                        self._put(db, "definition", definition.model_copy(update={
                            "enabled": False, "next_due": None, "revision": definition.revision + 1,
                            "updated_at": datetime.now(UTC),
                        }), definition.id)
                        for pending_row in db.execute("SELECT body FROM objects WHERE kind='occurrence'"):
                            pending = Occurrence.model_validate_json(pending_row[0])
                            if pending.automation_id == definition.id and pending.phase is Phase.QUEUED:
                                self._put(db, "occurrence", pending.model_copy(update={
                                    "phase": Phase.SETTLED, "result": Result.SKIPPED,
                                    "detail": "目标收尾时暂停持续跟进。尚未执行",
                                }), pending.id)
                session_id = existing.primary_session_id if existing else edit.source_session_id
                if closing and execution:
                    execution = execution.model_copy(update={
                        "status": (GoalExecutionStatus.ACCEPTED if edit.goal.status == "completed"
                                   else GoalExecutionStatus.PAUSED),
                        "detail": ("目标已由你确认完成。" if edit.goal.status == "completed"
                                   else "目标已归档。"),
                    })
                goal = Goal(
                    id=edit.id,
                    revision=revision + 1,
                    primary_session_id=session_id,
                    execution=execution,
                    **edit.goal.model_dump(),
                )
                self._put(db, "goal", goal, goal.id)
                if session_id and not existing:
                    self._bind_discussion(db, session_id, ContextRef(kind="goal", id=goal.id), created=True)
            elif edit.action == "edit_resource":
                resource = self._get(db, "resource", edit.id, Resource)
                if resource.kind == "work_product" or resource.file_path:
                    raise ValueError("Edit this source through its original conversation and TaskWorkspace")
                if resource.artifact_ref == artifact_ref and resource.title == edit.title:
                    return
                if resource.revision != edit.expected_revision:
                    raise AssistantConflict("Resource changed; reload before editing")
                if not artifact_ref or not edit.title.strip():
                    raise ValueError("A resource needs a title and content")
                self._put(
                    db,
                    "resource",
                    resource.model_copy(
                        update={
                            "title": edit.title.strip(),
                            "artifact_ref": artifact_ref,
                            "revision": resource.revision + 1,
                            "updated_at": datetime.now(UTC),
                        }
                    ),
                    resource.id,
                )
            elif edit.action == "note":
                if not artifact_ref or not edit.title.strip():
                    raise ValueError("A note needs a title and content")
                prior = db.execute(
                    "SELECT body FROM objects WHERE kind='resource' AND id=?", (edit.id,)
                ).fetchone()
                if prior:
                    existing = Resource.model_validate_json(prior[0])
                    if existing.artifact_ref == artifact_ref and existing.title == edit.title:
                        return
                    raise AssistantConflict("Resource identity already exists; use a versioned edit")
                resource = Resource(
                    id=edit.id,
                    title=edit.title,
                    kind="note",
                    artifact_ref=artifact_ref,
                    created_at=datetime.now(UTC),
                    saved=True,
                )
                self._put(db, "resource", resource, resource.id)
            else:
                db.execute(
                    "INSERT INTO objects VALUES('idea',?,?) "
                    "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                    (edit.id, json.dumps({"dismissed": edit.value})),
                )
            if edit.action in {"note", "edit_resource"}:
                db.execute(
                    "INSERT INTO objects VALUES('resource_search',?,?) "
                    "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                    (edit.id, json.dumps({"text": edit.content})),
                )

    def idea_dismissed(self, identity: str) -> bool:
        with self.transaction() as db:
            row = db.execute("SELECT body FROM objects WHERE kind='idea' AND id=?", (identity,)).fetchone()
            return bool(row and json.loads(row[0])["dismissed"])

    def snapshot(self, owner: bool, status: str, ideas: tuple[Idea, ...]) -> AssistantState:
        with self.transaction() as db:

            def rows(kind: str) -> list[dict[str, Any]]:
                return [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT body FROM objects WHERE kind=? "
                        "ORDER BY CASE WHEN json_extract(body,'$.phase') IN ('running','settling') "
                        "THEN 0 ELSE 1 END, rowid DESC LIMIT ?",
                        (kind, -1 if kind in {"definition", "goal"} else 200),
                    ).fetchall()
                ]

            feed = [
                {**json.loads(row[1]), "sequence": row[0]}
                for row in db.execute(
                    f"SELECT sequence,body FROM feed WHERE {_VISIBLE_FEED} ORDER BY sequence DESC LIMIT 100"
                )
            ]
            dismissed = {
                row[0]: json.loads(row[1])["dismissed"]
                for row in db.execute(
                    "SELECT id,body FROM objects WHERE kind='idea'",
                )
            }
            return AssistantState.model_validate(
                {
                    "owner": owner,
                    "status": status,
                    "definitions": rows("definition"),
                    "occurrences": [
                        {**item, "preference_snapshot": [], "spec_snapshot": None}
                        for item in rows("occurrence")
                    ],
                    "feed": feed,
                    "resources": rows("resource"),
                    "goals": rows("goal"),
                    "collections": rows("collection"),
                    "ideas": [
                        idea.model_copy(
                            update={
                                "dismissed": dismissed.get(idea.id, False),
                            }
                        )
                        for idea in ideas
                    ],
                }
            )

    def register_session(self, identity: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO owned_sessions VALUES(?)", (identity,))

    def owned_sessions(self) -> tuple[str, ...]:
        with self.transaction() as db:
            return tuple(str(row[0]) for row in db.execute("SELECT id FROM owned_sessions"))

    def index_work_products(
        self, session_id: str, products: tuple[WorkProductRef, ...], *, deleted: bool,
        output_interactions: dict[str, str] | None = None,
    ) -> None:
        with self.transaction() as db:
            for product in products:
                if product.current.content_ref is None:
                    continue
                identity = f"work:{session_id}:{product.id}"
                row = db.execute(
                    "SELECT body FROM objects WHERE kind='resource' AND id=?", (identity,)
                ).fetchone()
                previous = Resource.model_validate_json(row[0]) if row else None
                produced = (
                    product.id in (output_interactions or {}) and product.status.value == "active"
                    and not any(part.startswith(".") for part in Path(product.resource).parts)
                )
                changed = previous is not None and previous.artifact_ref != product.current.content_ref
                resource = Resource(
                    id=identity,
                    title=previous.title if previous else Path(product.resource).name,
                    kind="work_product",
                    artifact_ref=product.current.content_ref,
                    session_id=session_id,
                    created_at=datetime.fromisoformat(product.opened_at),
                    saved=previous.saved if previous else False,
                    published=previous.published if previous else produced,
                    revision=(previous.revision + int(changed)) if previous else 1,
                    updated_at=datetime.now(UTC) if changed else previous.updated_at if previous else None,
                    primary_session_id=previous.primary_session_id if previous else None,
                    work_product_id=product.id,
                    status="unavailable" if deleted else product.status.value,
                    file_path=product.resource,
                    file_type=resource_file_type(product.resource),
                    byte_size=product.current.byte_size,
                    interaction_id=(output_interactions or {}).get(product.id),
                    deleted_at=previous.deleted_at if previous else None,
                )
                self._put(db, "resource", resource, identity)

    def collect_file(self, resource: Resource, *, goal_ids: tuple[str, ...], text: str = "") -> Resource:
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM objects WHERE kind='resource' AND id=?", (resource.id,),
            ).fetchone()
            if row:
                previous = Resource.model_validate_json(row[0])
                resource = resource.model_copy(update={
                    "saved": previous.saved, "published": previous.published,
                    "created_at": previous.created_at, "primary_session_id": previous.primary_session_id,
                    "revision": previous.revision + int(previous.artifact_ref != resource.artifact_ref),
                    "updated_at": resource.updated_at if previous.artifact_ref != resource.artifact_ref
                    else previous.updated_at,
                    "deleted_at": previous.deleted_at, "title": previous.title,
                })
            self._put(db, "resource", resource, resource.id)
            db.execute(
                "INSERT INTO objects VALUES('resource_search',?,?) "
                "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                (resource.id, json.dumps({"text": text})),
            )
            self._record_output(db, resource, goal_ids)
            return resource

    def record_output(self, identity: str, *, goal_ids: tuple[str, ...]) -> None:
        with self.transaction() as db:
            self._record_output(db, self._get(db, "resource", identity, Resource), goal_ids)

    def _record_output(self, db: sqlite3.Connection, resource: Resource, goal_ids: tuple[str, ...]) -> None:
        for goal_id in goal_ids:
            self._get(db, "goal", goal_id, Goal)
            db.execute(
                "INSERT OR IGNORE INTO goal_resources VALUES(?,?,?)", (goal_id, resource.id, "output"),
            )
        if resource.deleted_at or not resource.published or resource.status != "active":
            return
        latest = db.execute(
            "SELECT json_extract(body,'$.resource_revision') FROM feed "
            "WHERE json_extract(body,'$.resource_id')=? ORDER BY sequence DESC LIMIT 1",
            (resource.id,),
        ).fetchone()
        if latest and latest[0] is not None:
            previous = db.execute(
                "SELECT body FROM resource_versions WHERE id=? AND revision=?", (resource.id, latest[0]),
            ).fetchone()
            if previous and Resource.model_validate_json(previous[0]).artifact_ref == resource.artifact_ref:
                return
        item = FeedItem(
            sequence=0, id=f"output:{resource.id}:{resource.revision}", title=resource.title,
            result=Result.COMPLETE,
            detail="新成果已就绪。打开查看或围绕这份成果继续讨论。" if resource.revision == 1
            else f"成果已更新至版本 {resource.revision}。此前版本仍可查看。",
            created_at=resource.updated_at or resource.created_at,
            resource_id=resource.id, resource_revision=resource.revision,
            goal_id=goal_ids[0] if goal_ids else None, session_id=resource.session_id,
        )
        db.execute("INSERT OR IGNORE INTO feed(id,body) VALUES(?,?)", (item.id, item.model_dump_json()))

    def import_file(self, resource: Resource, *, text: str) -> Resource:
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM objects WHERE kind='resource' AND id=?", (resource.id,),
            ).fetchone()
            if row:
                existing = Resource.model_validate_json(row[0])
                if existing.artifact_ref != resource.artifact_ref or existing.file_path != resource.file_path:
                    raise AssistantConflict("Import identity already belongs to a different file")
                return existing
            self._put(db, "resource", resource, resource.id)
            db.execute("INSERT INTO objects VALUES('resource_search',?,?)",
                       (resource.id, json.dumps({"text": text})))
            return resource

    def resource_version(self, identity: str, revision: int | None) -> Resource:
        with self.transaction() as db:
            current = self._get(db, "resource", identity, Resource)
            if revision is None or revision == current.revision:
                return current
            row = db.execute(
                "SELECT body FROM resource_versions WHERE id=? AND revision=?", (identity, revision)
            ).fetchone()
            if row is None:
                raise ValueError("Resource version is unavailable")
            return Resource.model_validate_json(row[0])

    def resource_links(self, identity: str) -> dict[str, list[str]]:
        with self.transaction() as db:
            return {
                "goal_ids": [
                    r[0]
                    for r in db.execute("SELECT goal_id FROM goal_resources WHERE resource_id=?", (identity,))
                ],
                "collection_ids": [
                    r[0]
                    for r in db.execute(
                        "SELECT collection_id FROM collection_resources WHERE resource_id=?", (identity,)
                    )
                ],
            }

    def goal_detail(self, identity: str) -> GoalDetail:
        with self.transaction() as db:
            goal = self._get(db, "goal", identity, Goal)
            linked = [
                (self._get(db, "resource", row[0], Resource), row[1])
                for row in db.execute(
                    "SELECT resource_id,role FROM goal_resources AS links JOIN objects "
                    "ON objects.kind='resource' AND objects.id=links.resource_id "
                    "WHERE goal_id=? AND json_extract(body,'$.deleted_at') IS NULL ORDER BY links.rowid DESC",
                    (identity,),
                )
            ]
            definitions = tuple(self._get(db, "definition", item, Definition) for item in goal.automation_ids)
            occurrences = tuple(
                Occurrence.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT body FROM objects WHERE kind='occurrence' "
                    "AND json_extract(body,'$.automation_id') "
                    "IN (SELECT value FROM json_each(?)) ORDER BY rowid DESC LIMIT 50",
                    (json.dumps(goal.automation_ids),),
                )
            )
            return GoalDetail(
                goal=goal,
                resources=tuple(r for r, role in linked if role == "output"),
                context_resources=tuple(r for r, role in linked if role == "context"),
                definitions=definitions,
                occurrences=occurrences,
            )

    def discussion(self, reference: ContextRef) -> str:
        with self.transaction() as db:
            if reference.kind == "session":
                if not db.execute("SELECT 1 FROM owned_sessions WHERE id=?", (reference.id,)).fetchone():
                    raise ValueError("Session is unavailable in this workspace")
                return reference.id
            model = Goal if reference.kind == "goal" else Resource
            current = self._get(db, reference.kind, reference.id, model)
            if isinstance(current, Goal) and current.status == "archived":
                raise ValueError("Restore this goal before continuing")
            identity = current.primary_session_id or str(uuid4())
            if current.primary_session_id is None:
                self._put(
                    db,
                    reference.kind,
                    current.model_copy(update={"primary_session_id": identity}),
                    reference.id,
                )
                self._bind_discussion(db, identity, reference, created=False)
                db.execute("INSERT OR IGNORE INTO owned_sessions VALUES(?)", (identity,))
            else:
                prior = db.execute(
                    "SELECT body FROM objects WHERE kind='discussion' AND id=?", (identity,)
                ).fetchone()
                created = json.loads(prior[0]).get("created", True) if prior else True
                self._bind_discussion(db, identity, reference, created=created)
            return identity

    def _bind_discussion(
        self, db: sqlite3.Connection, session_id: str, reference: ContextRef, *, created: bool,
    ) -> None:
        row = db.execute("SELECT body FROM objects WHERE kind='discussion' AND id=?",
                         (session_id,)).fetchone()
        body: dict[str, Any] = json.loads(row[0]) if row else {}
        refs = [ContextRef.model_validate(item)
                for item in body.get("contexts", [body["context"]] if "context" in body else [])]
        refs = [item for item in refs if (item.kind, item.id) != (reference.kind, reference.id)]
        refs.append(reference)
        if len(refs) > 16:
            raise ValueError("一个对话最多关联 16 项工作上下文")
        db.execute("INSERT INTO objects VALUES('discussion',?,?) "
            "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (session_id, json.dumps({
                "context": reference.model_dump(mode="json"),
                "contexts": [item.model_dump(mode="json") for item in refs], "created": created})))

    def discussion_created(self, session_id: str, *, mark: bool = False) -> bool:
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM objects WHERE kind='discussion' AND id=?", (session_id,)
            ).fetchone()
            if row is None:
                return True
            body = json.loads(row[0])
            created = bool(body.get("created", True))
            if mark:
                db.execute(
                    "UPDATE objects SET body=? WHERE kind='discussion' AND id=?",
                    (json.dumps({**body, "created": True}), session_id),
                )
            return created

    def discussion_context(self, session_id: str) -> tuple[ContextRef, ...]:
        with self.transaction() as db:
            row = db.execute(
                "SELECT body FROM objects WHERE kind='discussion' AND id=?", (session_id,)
            ).fetchone()
            if not row:
                return ()
            body = json.loads(row[0])
            refs = tuple(ContextRef.model_validate(item)
                         for item in body.get("contexts", [body["context"]]))
            visible: list[ContextRef] = []
            for reference in refs:
                row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?",
                                 (reference.kind, reference.id)).fetchone()
                if row and json.loads(row[0]).get("deleted_at") is None:
                    visible.append(reference)
            return tuple(visible)

    def control_goal(self, command: GoalCommand) -> bool:
        with self.transaction() as db:
            identity = f"goal:{command.request_id}"
            payload = digest(command.model_dump(mode="json"))
            prior = db.execute("SELECT digest,result FROM requests WHERE id=?", (identity,)).fetchone()
            if prior:
                if prior[0] != payload:
                    raise AssistantConflict("目标操作标识已用于其他内容")
                return False
            goal = self._get(db, "goal", command.goal_id, Goal)
            if goal.revision != command.expected_revision:
                    raise AssistantConflict("目标已更新。请重新读取")
            execution = goal.execution
            if command.action == "pause":
                if execution is None:
                    raise ValueError("目标尚未启动推进")
                execution = execution.model_copy(update={
                    "status": GoalExecutionStatus.PAUSED, "detail": "已暂停。由你决定何时继续。",
                })
            else:
                if goal.status != "active":
                    raise ValueError("请先将目标恢复为未完成状态")
                if execution and execution.status is GoalExecutionStatus.ACTIVE:
                    raise AssistantConflict("目标已经在推进")
                if command.action == "resume" and execution is None:
                    raise ValueError("目标尚未启动推进")
                criteria = command.criteria.strip() or (execution.criteria if execution else "")
                if not criteria:
                    raise ValueError("启动前请明确可验证的成功标准")
                turns = execution.turns if execution and command.action == "resume" else 0
                if command.turn_limit is not None and command.turn_limit <= turns:
                    raise ValueError("轮数上限已用完。请提高上限后继续")
                execution = GoalExecution(id=command.request_id, status=GoalExecutionStatus.ACTIVE,
                    criteria=criteria, turn_limit=command.turn_limit, turns=turns,
                    evidence=execution.evidence if execution else ())
            self._put(db, "goal", goal.model_copy(update={
                "execution": execution, "revision": goal.revision + 1,
            }), goal.id)
            db.execute("INSERT INTO requests VALUES(?,?,?)", (identity, payload, goal.id))
            return True

    def admit_goal_run(self, goal_id: str, activation_id: str, run_id: str) -> None:
        with self.transaction() as db:
            goal = self._get(db, "goal", goal_id, Goal)
            execution = goal.execution
            if (goal.status != "active" or execution is None or execution.id != activation_id
                    or execution.status is not GoalExecutionStatus.ACTIVE):
                raise AssistantConflict("目标推进已停止或范围已变化")
            if execution.turn_limit is not None and execution.turns >= execution.turn_limit:
                raise AssistantConflict("目标推进轮数上限已用完")
            self._put(db, "goal", goal.model_copy(update={"revision": goal.revision + 1,
                "execution": execution.model_copy(update={"turns": execution.turns + 1,
                    "run_id": run_id, "detail": "正在执行并核对成功标准。"})}), goal.id)

    def finish_goal_run(
        self, goal_id: str, activation_id: str, status: GoalExecutionStatus, detail: str,
        *, run_id: str | None = None, evidence: GoalEvidence | None = None,
    ) -> bool:
        with self.transaction() as db:
            goal = self._get(db, "goal", goal_id, Goal)
            execution = goal.execution
            if execution is None or execution.id != activation_id:
                return False
            if run_id and execution.run_id != run_id:
                return False
            if execution.status is not GoalExecutionStatus.ACTIVE:
                return False
            if (status is GoalExecutionStatus.ACTIVE and execution.turn_limit is not None
                    and execution.turns >= execution.turn_limit):
                status = GoalExecutionStatus.BUDGET_REACHED
                detail = "已达到推进轮数上限。请检查进度后决定是否继续。"
            records = (*execution.evidence, evidence)[-20:] if evidence else execution.evidence
            self._put(db, "goal", goal.model_copy(update={"revision": goal.revision + 1,
                "execution": execution.model_copy(update={"status": status,
                    "detail": detail[:3000], "evidence": records})}), goal.id)
            if status in {GoalExecutionStatus.REVIEW, GoalExecutionStatus.BLOCKED,
                          GoalExecutionStatus.BUDGET_REACHED}:
                action_label = "验收成果" if status is GoalExecutionStatus.REVIEW else "检查进展"
                item = FeedItem(
                    sequence=0,
                    id=f"goal-attention:{goal.id}:{activation_id}:{execution.run_id or 'pending'}",
                    title=f"{action_label} · {goal.title}",
                    result=Result.NEEDS_USER_ACTION, detail=detail[:3000],
                    created_at=datetime.now(UTC), goal_id=goal.id, session_id=goal.primary_session_id,
                )
                db.execute(
                    "INSERT OR IGNORE INTO feed(id,body) VALUES(?,?)", (item.id, item.model_dump_json()),
                )
            return status is GoalExecutionStatus.ACTIVE

    def interrupt_goals(self) -> None:
        with self.transaction() as db:
            for row in db.execute("SELECT body FROM objects WHERE kind='goal'").fetchall():
                goal = Goal.model_validate_json(row[0])
                if goal.execution and goal.execution.status is GoalExecutionStatus.ACTIVE:
                    self._put(db, "goal", goal.model_copy(update={"revision": goal.revision + 1,
                        "execution": goal.execution.model_copy(update={
                            "status": GoalExecutionStatus.PAUSED,
                            "detail": "服务重启或执行中断。已保留进度。请核对后明确恢复。",
                        })}), goal.id)

    def record_run(self, identity: str, session_id: str, request_id: str, context: dict[str, Any]) -> str:
        with self.transaction() as db:
            prior = db.execute(
                "SELECT id,body FROM assistant_runs WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            body = json.dumps(context, sort_keys=True)
            if prior:
                if prior[1] != body:
                    raise AssistantConflict("Run request identity already has another context")
                return str(prior[0])
            db.execute("INSERT INTO assistant_runs VALUES(?,?,?,?)", (identity, session_id, request_id, body))
            return identity

    def publish(self, resource: Resource, *, goal_id: str | None = None, text: str = "") -> Resource:
        with self.transaction() as db:
            if goal_id:
                self._get(db, "goal", goal_id, Goal)
                db.execute(
                    "INSERT OR REPLACE INTO goal_resources VALUES(?,?,?)", (goal_id, resource.id, "output")
                )
            row = db.execute(
                "SELECT body FROM objects WHERE kind='resource' AND id=?", (resource.id,)
            ).fetchone()
            if row:
                return Resource.model_validate_json(row[0])
            self._put(db, "resource", resource, resource.id)
            db.execute(
                "INSERT INTO objects VALUES('resource_search',?,?)", (resource.id, json.dumps({"text": text}))
            )
            item = FeedItem(
                sequence=0,
                id=f"publish:{resource.id}",
                title=resource.title,
                result=Result.COMPLETE,
                detail="成果已保存。可以继续讨论或修改。",
                created_at=resource.created_at,
                resource_id=resource.id,
                resource_revision=resource.revision,
                goal_id=goal_id,
            )
            db.execute("INSERT OR IGNORE INTO feed(id,body) VALUES(?,?)", (item.id, item.model_dump_json()))
            return resource
