"""SQLite projection contracts, including concurrent Host and crash recovery paths."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lumen.context.memory.records import MemoryKind, MemoryRecord, MemoryScope, MemorySource, Sensitivity
from lumen.context.memory.repository import SQLiteMemoryRepository
from lumen.context.memory.viking_store import VikingJob, VikingStore

ENDPOINT = "https://viking.example"
PROJECT = "project-a"
CREDENTIAL_HASH = "a" * 64


def memory(identity: str = "fact", text: str = "stage internal release first") -> MemoryRecord:
    now = datetime.now(UTC)
    return MemoryRecord(
        id=identity,
        scope=MemoryScope.PROJECT,
        project_id=PROJECT,
        kind=MemoryKind.WORKFLOW,
        content=text,
        created_at=now,
        updated_at=now,
        valid_from=now,
    )


def bind(store: VikingStore, *, user_scope: bool = False) -> str:
    return store.bind(
        ENDPOINT, "org", "alice", PROJECT, user_scope=user_scope, credential_hash=CREDENTIAL_HASH
    )


def take(store: VikingStore, binding: str, op: str) -> VikingJob:
    job = store.claim(binding, PROJECT)
    assert job and job.op == op
    return job


def test_authority_transaction_rolls_back_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    binding = bind(repo.viking)

    def fail(*_: object) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(repo.viking, "changed", fail)
    with pytest.raises(RuntimeError):
        repo.remember(memory())
    assert repo.get("fact") is None
    assert repo.viking.claim(binding, PROJECT) is None


def test_immutable_versions_private_and_offline_mutations(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    store = repo.viking
    binding = bind(store)
    record = repo.remember(memory())
    first = take(store, binding, "write")
    assert store.validate(first) == record.content
    store.finish(first)
    repo.mark_used(record.id)
    assert store.claim(binding, PROJECT) is None
    assert store.roots(binding, PROJECT, [record.id]) == []
    assert store.roots(binding, PROJECT, []) == [first.uri.rsplit("/", 2)[0]]
    assert [r.id for r in store.resolve(binding, PROJECT, [first.uri, first.uri + "/x", first.uri])] == [
        "fact"
    ]
    assert not store.resolve(binding, "project-b", [first.uri])
    repo.remember(record.model_copy(update={"content": "edited release"}))
    assert not store.resolve(binding, PROJECT, [first.uri])
    deletion = take(store, binding, "delete")
    assert deletion.uri == first.uri and store.validate(deletion) == ""
    store.finish(deletion)
    edit = take(store, binding, "write")
    assert edit.uri != first.uri and store.validate(edit) == "edited release"
    # Another Host with no active service may still invalidate a version, but cannot enqueue an upload.
    offline = SQLiteMemoryRepository(path)
    offline.remember(record.model_copy(update={"content": "offline edit"}))
    assert store.validate(edit) is None
    store.finish(edit)
    cleanup = take(store, binding, "delete")
    store.finish(cleanup)
    assert not store.resolve(binding, PROJECT, [first.uri, edit.uri])
    store.private = True
    repo.remember(record.model_copy(update={"content": "private edit"}))
    assert not store.eligible_count(PROJECT)
    store.private = False
    assert not store.eligible_count(PROJECT)
    store.backfill(binding, PROJECT)
    assert store.status(binding, PROJECT)["backfill"] == 0
    assert store.claim(binding, PROJECT) is None


def test_backfill_grants_scope_and_privacy(tmp_path: Path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    store = repo.viking
    for r in (
        memory(),
        memory("global").model_copy(update={"scope": MemoryScope.USER, "project_id": None}),
        memory("inferred").model_copy(update={"source_kind": MemorySource.INFERRED}),
        memory("restricted").model_copy(update={"sensitivity": Sensitivity.RESTRICTED}),
        memory("other").model_copy(update={"project_id": "project-b"}),
    ):
        repo.remember(r)
    binding = bind(store)
    assert store.grants(binding, PROJECT) == (True, False)
    assert store.lookup(ENDPOINT, "org", "bob", PROJECT) is None
    assert store.claim(binding, PROJECT) is None
    assert store.eligible_count(PROJECT) == 1 and store.eligible_count(PROJECT, user_scope=True) == 1
    store.backfill(binding, PROJECT)
    store.backfill(binding, PROJECT)
    assert store.status(binding, PROJECT)["backfill"] == 1
    item = take(store, binding, "write")
    store.finish(item)
    assert store.status(binding, PROJECT)["ready"] == 1
    with pytest.raises(ValueError):
        store.backfill(binding, PROJECT, user_scope=True)
    bind(store, user_scope=True)
    store.backfill(binding, PROJECT, user_scope=True)
    assert store.status(binding, PROJECT, user_scope=True)["backfill"] == 1
    user = take(store, binding, "write")
    store.finish(user)
    assert store.resolve(binding, "project-b", [user.uri])[0].id == "global"
    store.disable(binding, PROJECT, user_scope=True)
    assert not store.resolve(binding, "project-b", [user.uri])
    assert store.resolve(binding, PROJECT, [item.uri])
    store.disable(binding, PROJECT)
    assert not store.has_work(binding, PROJECT)


def test_independent_hosts_lease_recovery_and_late_write(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    first = SQLiteMemoryRepository(path)
    second = SQLiteMemoryRepository(path)
    binding = bind(first.viking)
    connected = second.viking.connect(ENDPOINT, "org", "alice", PROJECT, credential_hash=CREDENTIAL_HASH)
    assert connected == binding
    second.remember(memory())
    old = take(first.viking, binding, "write")
    assert second.viking.claim(binding, PROJECT) is None
    second.viking.disable(binding, PROJECT, purge=True)
    assert first.viking.validate(old) is None
    assert first.viking.resolve(binding, PROJECT, [old.uri]) == []
    with sqlite3.connect(path) as db:
        db.execute("UPDATE ov_object SET lease_end=0")
    new = take(second.viking, binding, "delete")
    assert new.lease != old.lease
    second.viking.finish(new)
    # Original process resumes after lease expiry, after the winning worker has cleaned the URI.
    first.viking.finish(old)
    cleanup = take(second.viking, binding, "delete")
    assert new.uri == old.uri == cleanup.uri
    second.viking.finish(cleanup)
    assert first.viking.status(binding, PROJECT)["cleanup"] == 0


@pytest.mark.parametrize("remove", [SQLiteMemoryRepository.forget, SQLiteMemoryRepository.expire])
def test_delete_survives_restart(
    tmp_path: Path, remove: Callable[[SQLiteMemoryRepository, str], bool]
) -> None:
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    binding = bind(repo.viking)
    repo.remember(memory())
    first = take(repo.viking, binding, "write")
    repo.viking.finish(first)
    assert remove(repo, "fact")
    repo.close()
    restarted = SQLiteMemoryRepository(path)
    connected = restarted.viking.connect(ENDPOINT, "org", "alice", PROJECT, credential_hash=CREDENTIAL_HASH)
    assert connected == binding
    assert not restarted.viking.resolve(binding, PROJECT, [first.uri])
    assert take(restarted.viking, binding, "delete").uri == first.uri


def test_expired_at_ingest_and_natural_expiry(tmp_path: Path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    binding = bind(repo.viking)
    now = datetime.now(UTC)
    repo.remember(
        memory().model_copy(
            update={"valid_from": now - timedelta(days=2), "valid_until": now - timedelta(days=1)}
        )
    )
    assert repo.viking.claim(binding, PROJECT) is None
    repo.remember(memory())
    job = take(repo.viking, binding, "write")
    repo.viking.finish(job)
    with repo.viking.transaction() as db:
        db.execute("UPDATE memory SET valid_until='2000-01-01T00:00:00Z' WHERE id='fact'")
    assert take(repo.viking, binding, "delete").uri == job.uri


def test_restoring_stale_authority_requires_fresh_grant(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    project = bind(repo.viking)
    repo.remember(memory())
    item = take(repo.viking, project, "write")
    repo.viking.finish(item)
    backup = tmp_path / "earlier.sqlite3"
    shutil.copyfile(path, backup)
    repo.forget("fact")
    repo.close()
    shutil.copyfile(backup, path)
    restored = SQLiteMemoryRepository(path)
    assert restored.get("fact") is not None
    assert restored.viking.grants(project, PROJECT) == (False, False)
    assert not restored.viking.resolve(project, PROJECT, [item.uri])
    assert restored.viking.claim(project, PROJECT) is None


def test_backfill_resumes_pages_but_rechecks_each_version(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    for i in range(70):
        repo.remember(memory(f"entry-{i:02}"))
    binding = bind(repo.viking)
    repo.viking.backfill(binding, PROJECT)
    assert repo.viking.status(binding, PROJECT)["backfill"] == 70
    first = take(repo.viking, binding, "write")
    repo.viking.finish(first)
    repo.close()
    resumed = SQLiteMemoryRepository(path)
    resumed.forget("entry-69")
    total = 1
    while job := resumed.viking.claim(binding, PROJECT):
        assert job.op == "write" and resumed.viking.validate(job)
        resumed.viking.finish(job)
        total += 1
    assert total == 69


def test_offline_restart_stages_only_verified_key_endpoint_and_project(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    approved, other = "a" * 64, "b" * 64
    initial = SQLiteMemoryRepository(path)
    binding = initial.viking.bind(ENDPOINT, "org", "alice", PROJECT, credential_hash=approved)
    initial.close()

    offline = SQLiteMemoryRepository(path)
    offline.viking.select_writer(ENDPOINT, other, PROJECT)
    offline.remember(memory("unverified"))
    offline.viking.select_writer("https://different.example", approved, PROJECT)
    offline.remember(memory("different-origin"))
    offline.viking.select_writer(ENDPOINT, approved, "other-project")
    offline.remember(memory("wrong-project"))
    assert offline.viking.claim(binding, PROJECT) is None
    offline.viking.select_writer(ENDPOINT, approved, PROJECT)
    offline.remember(memory("eligible", "capture before verification"))
    offline.close()

    online = SQLiteMemoryRepository(path)
    assert online.viking.connect(ENDPOINT, "org", "alice", PROJECT, credential_hash=approved) == binding
    job = take(online.viking, binding, "write")
    assert online.viking.validate(job) == "capture before verification"
    online.viking.finish(job)
    assert online.viking.claim(binding, PROJECT) is None
    online.viking.disable(binding, PROJECT)
    offline.viking.select_writer(ENDPOINT, approved, PROJECT)
    offline.remember(memory("after-revocation"))
    assert not online.viking.has_work(binding, PROJECT)
    assert online.viking.status(binding, PROJECT)["pending"] == 0


def test_completed_purge_skips_reconnect_until_audit_due(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    binding = bind(repo.viking)
    repo.remember(memory())
    repo.viking.finish(take(repo.viking, binding, "write"))
    repo.viking.disable(binding, PROJECT, purge=True)
    assert repo.viking.has_work(binding, PROJECT)
    deletion = take(repo.viking, binding, "delete")
    repo.viking.finish(deletion)
    repo.close()
    reopened = SQLiteMemoryRepository(path)
    assert not reopened.viking.has_work(binding, PROJECT)
    with reopened.viking.transaction() as db:
        db.execute("UPDATE ov_object SET due=0 WHERE id=?", (deletion.id,))
    assert reopened.viking.has_work(binding, PROJECT)
    audit = take(reopened.viking, binding, "delete")
    assert audit.uri == deletion.uri and reopened.viking.validate(audit) == ""


def test_ready_audit_repairs_missing_version_but_never_revives_forgotten(tmp_path: Path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    store = repo.viking
    binding = bind(store)
    for identity in ("first", "second"):
        repo.remember(memory(identity))
        store.finish(take(store, binding, "write"))
    assert store.claim(binding, PROJECT) is None
    with store.transaction() as db:
        db.execute("UPDATE ov_object SET due=0")
    audit = take(store, binding, "verify")
    assert store.validate(audit) == ""
    # Other Hosts must also respect the binding-wide audit rate limit.
    assert store.claim(binding, PROJECT) is None
    store.finish(audit, missing=True)
    repair = take(store, binding, "write")
    assert repair.uri == audit.uri and store.validate(repair) == memory().content
    store.finish(repair)
    with store.transaction() as db:
        db.execute("UPDATE ov_audit SET next_audit=0")
    late = take(store, binding, "verify")
    assert late.uri != repair.uri
    repo.forget("second")
    assert store.validate(late) is None
    store.finish(late, missing=True)
    assert not store.resolve(binding, PROJECT, [late.uri])
    assert take(store, binding, "delete").uri == late.uri


def test_status_counts_and_retry_health_are_scoped_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    repo = SQLiteMemoryRepository(path)
    store = repo.viking
    repo.remember(memory("personal").model_copy(update={"scope": MemoryScope.USER, "project_id": None}))
    binding = bind(store)
    repo.remember(memory())
    bind(store, user_scope=True)
    store.backfill(binding, PROJECT, user_scope=True)
    personal = store.status(binding, PROJECT, user_scope=True)
    assert personal["pending"] == personal["backfill"] == 1
    project = store.status(binding, PROJECT)
    assert project["pending"] == 1 and project["backfill"] == 0
    job = take(store, binding, "write")
    assert "/projects/" in job.uri
    store.finish(job, error="http_503")
    repo.close()
    repo = SQLiteMemoryRepository(path)
    project = repo.viking.status(binding, PROJECT)
    assert project["last_error"] == "http_503" and project["consecutive_errors"] == 1
    personal = repo.viking.status(binding, PROJECT, user_scope=True)
    assert personal["last_error"] is None and personal["consecutive_errors"] == 0
    assert float(str(project["oldest_pending_seconds"])) >= 0


def test_legacy_fts_postings_are_rebuilt_and_stay_in_step_with_authority(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    original = memory(text="originalkeyword")
    with sqlite3.connect(path) as db:
        db.executescript(SQLiteMemoryRepository.schema)
        db.execute(
            "INSERT INTO memory VALUES(?,?,?,?,?,?,?,?,?)",
            (
                original.id,
                original.scope,
                original.kind,
                original.content,
                original.model_dump_json(),
                original.status,
                original.confidence,
                None,
                None,
            ),
        )
        # An old trigger appended postings under FTS rowids unrelated to the authority rowids.
        db.execute("INSERT INTO memory_fts(rowid,id,content) VALUES(67,?,?)", (original.id, original.content))
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("SELECT id FROM memory_fts WHERE memory_fts MATCH 'originalkeyword'").fetchall()
    repo = SQLiteMemoryRepository(path)
    assert repo.query("originalkeyword")[0].id == original.id
    repo.remember(memory("another", "otherkeyword"))
    for i in range(5):
        repo.mark_used(original.id)
        edited = repo.remember(original.model_copy(update={"content": f"updatedkeyword{i}"}))
        assert [r.id for r in repo.query(edited.content)] == [original.id]
    assert repo.query("originalkeyword") == []
    repo.forget(original.id)
    assert repo.query("updatedkeyword") == []
    assert repo.query("otherkeyword")[0].id == "another"
    # FTS5 rank=1 compares the full inverted index with the external content table.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO memory_fts(memory_fts,rank) VALUES('integrity-check',1)")
