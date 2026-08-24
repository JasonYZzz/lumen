"""Tests for the durable memory subsystem (M5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.config import ContextConfig
from lumen.context.engine import (
    AgentRef,
    ContextCommit,
    ContextEngine,
    ContextMemoryCommand,
    ContextRequest,
    RuntimeContextSnapshot,
    SessionRef,
    TaskSnapshot,
)
from lumen.context.memory import (
    InMemoryMemoryRepository,
    MemoryKind,
    MemoryManager,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    Sensitivity,
    SQLiteMemoryRepository,
    render_memory_index,
)
from lumen.context.memory.records import MemoryRecord
from lumen.plan import PlanState


def _record(
    *,
    id: str = "mem-test",
    content: str = "use uv run pytest",
    scope: MemoryScope = MemoryScope.PROJECT,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    confidence: float = 1.0,
    path_glob: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    project_id: str | None = "project-a",
) -> MemoryRecord:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    start = valid_from or now
    return MemoryRecord(
        id=id,
        scope=scope,
        kind=MemoryKind.WORKFLOW,
        content=content,
        source_kind=MemorySource.EXPLICIT,
        confidence=confidence,
        created_at=now,
        updated_at=now,
        valid_from=start,
        valid_until=valid_until,
        status=status,
        path_glob=path_glob,
        sensitivity=sensitivity,
        project_id=project_id,
    )


# --------------------------------------------------------------------------- #
# MemoryRecord invariants
# --------------------------------------------------------------------------- #


def test_path_scope_requires_path_glob() -> None:
    with pytest.raises(ValidationError, match="path_glob"):
        _record(scope=MemoryScope.PATH, path_glob=None)


def test_valid_until_must_be_after_valid_from() -> None:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    with pytest.raises(ValidationError, match="valid_until"):
        _record(valid_until=now - timedelta(days=1))


def test_record_round_trips() -> None:
    record = _record()
    restored = MemoryRecord.model_validate_json(record.model_dump_json())
    assert restored == record


# --------------------------------------------------------------------------- #
# Repository: tombstone, conflict, index, query
# --------------------------------------------------------------------------- #


def test_tombstone_prevents_resurrection_in_memory() -> None:
    """A forgotten record cannot be re-remembered (plan §11.6)."""

    repo = InMemoryMemoryRepository()
    repo.remember(_record(content="fact A"))
    assert repo.forget("mem-test") is True
    # Re-remembering the same id is refused; the tombstone stays.
    repo.remember(_record(content="fact A revived"))
    forgotten = repo.get("mem-test")
    assert forgotten is not None
    assert forgotten.status is MemoryStatus.FORGOTTEN


def test_shared_topic_does_not_imply_a_memory_conflict() -> None:
    """Lexical overlap alone is not evidence that two facts contradict."""

    repo = InMemoryMemoryRepository()
    repo.remember(_record(id="mem-1", content="always use ruff for linting"))
    repo.remember(_record(id="mem-2", content="always use black for linting"))
    assert {record.status for record in repo.list()} == {MemoryStatus.ACTIVE}


def test_index_excludes_conflicted_low_confidence_and_expired() -> None:
    repo = InMemoryMemoryRepository()
    repo.remember(_record(id="active", content="a unique active fact", confidence=0.9))
    repo.remember(_record(id="low", content="low confidence factoid", confidence=0.2))
    past = datetime(2026, 7, 24, tzinfo=UTC) - timedelta(days=1)
    long_ago = datetime(2026, 7, 24, tzinfo=UTC) - timedelta(days=2)
    repo.remember(_record(id="expired", content="an expired thing", valid_from=long_ago, valid_until=past))
    index_ids = {r.id for r in repo.index()}
    assert "active" in index_ids
    assert "low" not in index_ids
    assert "expired" not in index_ids


def test_query_ranks_by_relevance() -> None:
    repo = InMemoryMemoryRepository()
    repo.remember(_record(id="m1", content="prefer 2-space indent in python"))
    repo.remember(_record(id="m2", content="deploy with uv run pytest first"))
    hits = repo.query("indent python", limit=5)
    assert hits and hits[0].id == "m1"


def test_query_can_recall_low_confidence_record_not_loaded_in_fixed_index() -> None:
    repo = InMemoryMemoryRepository()
    repo.remember(_record(id="low", content="rare frobnicator workflow", confidence=0.2))

    assert repo.index() == []
    assert [record.id for record in repo.query("frobnicator")] == ["low"]


def test_sqlite_repository_round_trips_and_forgets(tmp_path: Path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repo.remember(_record(content="a sqlite-backed fact"))
    assert repo.get("mem-test") is not None
    assert repo.forget("mem-test") is True
    forgotten = repo.get("mem-test")
    assert forgotten is not None
    assert forgotten.status is MemoryStatus.FORGOTTEN
    repo.close()


def test_sqlite_fts_query_finds_content(tmp_path: Path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repo.remember(_record(id="fts1", content="configure ruff for line length"))
    repo.remember(_record(id="fts2", content="deploy to production with care"))
    hits = repo.query("ruff", limit=5)
    assert any(r.id == "fts1" for r in hits)
    repo.close()


def test_sqlite_repository_does_not_infer_conflict_from_shared_words(tmp_path: Path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    repo.remember(_record(id="ruff", content="always use ruff for linting"))
    repo.remember(_record(id="black", content="always use black for linting"))

    assert {record.status for record in repo.list()} == {MemoryStatus.ACTIVE}
    assert {record.id for record in repo.index()} == {"ruff", "black"}
    repo.close()


# --------------------------------------------------------------------------- #
# MemoryManager
# --------------------------------------------------------------------------- #


def test_manager_remember_is_idempotent_by_content() -> None:
    mgr = MemoryManager(InMemoryMemoryRepository())
    r1 = mgr.remember("use 2-space indent", scope=MemoryScope.PROJECT)
    r2 = mgr.remember("use 2-space indent", scope=MemoryScope.PROJECT)
    assert r1.id == r2.id  # same scope+content -> same id


def test_manager_forget_by_substring_tombstones_matches() -> None:
    mgr = MemoryManager(InMemoryMemoryRepository())
    mgr.remember("always run uv run pytest", scope=MemoryScope.PROJECT)
    mgr.remember("deploy with caution", scope=MemoryScope.PROJECT)
    count = mgr.forget("pytest")
    assert count == 1
    assert mgr.list() == [] or all("pytest" not in r.content for r in mgr.list())


def test_manager_recall_disabled_returns_empty() -> None:
    """use=False is the privacy switch: no memory blocks this turn (plan §11.6)."""

    mgr = MemoryManager(InMemoryMemoryRepository(), use=False)
    mgr.remember("a fact about python", scope=MemoryScope.PROJECT)
    assert mgr.recall("python") == []
    assert mgr.index() == []


def test_project_memories_are_isolated_by_repository_identity() -> None:
    repo = InMemoryMemoryRepository()
    project_a = MemoryManager(repo, project_id="repo-a")
    project_b = MemoryManager(repo, project_id="repo-b")
    project_a.remember("only project A uses bazel", scope=MemoryScope.PROJECT)

    assert [record.content for record in project_a.recall("bazel")] == ["only project A uses bazel"]
    assert project_b.recall("bazel") == []
    assert project_b.index() == []


def test_same_content_has_distinct_project_identity_and_cannot_be_forgotten_cross_project() -> None:
    repo = InMemoryMemoryRepository()
    project_a = MemoryManager(repo, project_id="repo-a")
    project_b = MemoryManager(repo, project_id="repo-b")

    record_a = project_a.remember("run the shared build command")
    record_b = project_b.remember("run the shared build command")

    assert record_a.id != record_b.id
    assert project_b.forget(record_a.id) == 0
    assert [record.id for record in project_a.list()] == [record_a.id]
    assert [record.id for record in project_b.list()] == [record_b.id]


def test_explicit_memory_rejects_secrets() -> None:
    manager = MemoryManager(InMemoryMemoryRepository(), project_id="repo-a")

    with pytest.raises(ValueError, match="sensitive"):
        manager.remember("API_KEY=sk-1234567890abcdefghijklmnop")


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #


def test_projection_excludes_restricted_and_groups_by_scope() -> None:
    records = [
        _record(id="pub", content="a public fact", scope=MemoryScope.PROJECT),
        _record(
            id="secret",
            content="api key location",
            scope=MemoryScope.PROJECT,
            sensitivity=Sensitivity.RESTRICTED,
        ),
        _record(id="usr", content="a user preference", scope=MemoryScope.USER),
    ]
    text = render_memory_index(records)
    assert "a public fact" in text
    assert "a user preference" in text
    assert "api key location" not in text  # restricted never projected


def test_projection_removes_stale_topic_after_last_record_is_forgotten(tmp_path: Path) -> None:
    manager = MemoryManager(
        InMemoryMemoryRepository(),
        project_id="repo-a",
        projection_dir=tmp_path / "memory",
    )
    record = manager.remember("use the project formatter", kind=MemoryKind.PREFERENCE)
    topic = tmp_path / "memory" / "topics" / "preference.md"
    assert topic.is_file()

    assert manager.forget(record.id) == 1
    assert not topic.exists()


# --------------------------------------------------------------------------- #
# Engine /memory control
# --------------------------------------------------------------------------- #


def _engine_with_memory() -> ContextEngine:
    def function(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content='{"goals":[]}')])

    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=100, keep_recent_tokens=2000, summary_max_tokens=2000),
        model=FunctionModel(function=function),
    )
    engine.memory = MemoryManager(InMemoryMemoryRepository())
    return engine


async def _noop(_event: object) -> None:
    return None


async def test_control_memory_remember_forget_list_rebuild() -> None:
    engine = _engine_with_memory()

    remembered = await engine.control(
        ContextMemoryCommand(action="remember", payload={"content": "always run ruff", "scope": "project"}),
        _noop,
    )
    assert remembered.status == "ok"
    assert remembered.payload["status"] == "active"

    listed = await engine.control(ContextMemoryCommand(action="list"), _noop)
    assert listed.status == "ok"
    assert len(listed.payload["records"]) == 1

    forgotten = await engine.control(ContextMemoryCommand(action="forget", payload={"target": "ruff"}), _noop)
    assert forgotten.status == "ok"
    assert forgotten.payload["count"] == 1
    # forget is a tombstone: re-remembering the same fact does not resurrect it.
    again = await engine.control(
        ContextMemoryCommand(action="remember", payload={"content": "always run ruff", "scope": "project"}),
        _noop,
    )
    assert again.payload["status"] == "forgotten"

    rebuilt = await engine.control(ContextMemoryCommand(action="rebuild"), _noop)
    assert rebuilt.status == "ok"
    assert "no model call" in rebuilt.message


async def test_control_memory_use_toggles_recall() -> None:
    engine = _engine_with_memory()
    await engine.control(ContextMemoryCommand(action="remember", payload={"content": "a python fact"}), _noop)
    off = await engine.control(ContextMemoryCommand(action="use", payload={"enabled": False}), _noop)
    assert off.payload["use"] is False
    memory = engine.memory
    assert memory is not None
    assert memory.recall("python") == []


async def test_control_memory_edit_exports_validates_and_applies_draft(tmp_path: Path) -> None:
    manager = MemoryManager(
        InMemoryMemoryRepository(),
        project_id="repo-a",
        projection_dir=tmp_path / "memory",
    )
    engine = _engine_with_memory()
    engine.memory = manager
    remembered = await engine.control(
        ContextMemoryCommand(
            action="remember",
            payload={"content": "always run the old command", "scope": "project"},
        ),
        _noop,
    )
    record_id = remembered.payload["id"]

    exported = await engine.control(
        ContextMemoryCommand(action="edit", payload={"target": record_id}),
        _noop,
    )

    assert exported.status == "ok"
    draft_path = Path(exported.payload["draft_path"])
    assert await asyncio.to_thread(draft_path.is_file)
    draft_mode = await asyncio.to_thread(lambda: draft_path.stat().st_mode)
    assert draft_mode & 0o777 == 0o600
    await asyncio.to_thread(
        draft_path.write_text,
        str(exported.payload["draft"]).replace("always run the old command", "always run uv test"),
        encoding="utf-8",
    )

    applied = await engine.control(
        ContextMemoryCommand(action="edit", payload={"target": record_id, "apply": True}),
        _noop,
    )

    assert applied.status == "ok"
    assert applied.payload["id"] == record_id
    assert [record.content for record in manager.recall("uv test")] == ["always run uv test"]
    assert not await asyncio.to_thread(draft_path.exists)


async def test_control_memory_edit_rejects_invalid_yaml_without_mutation(tmp_path: Path) -> None:
    manager = MemoryManager(
        InMemoryMemoryRepository(),
        project_id="repo-a",
        projection_dir=tmp_path / "memory",
    )
    engine = _engine_with_memory()
    engine.memory = manager
    remembered = await engine.control(
        ContextMemoryCommand(action="remember", payload={"content": "keep this fact"}),
        _noop,
    )
    record_id = remembered.payload["id"]
    exported = await engine.control(
        ContextMemoryCommand(action="edit", payload={"target": record_id}),
        _noop,
    )
    draft_path = Path(exported.payload["draft_path"])
    await asyncio.to_thread(
        draft_path.write_text,
        "---\ninvalid: [\n---\nreplace this fact\n",
        encoding="utf-8",
    )

    result = await engine.control(
        ContextMemoryCommand(action="edit", payload={"target": record_id, "apply": True}),
        _noop,
    )

    assert result.status == "error"
    assert [record.content for record in manager.list()] == ["keep this fact"]


async def test_prepare_reinjects_memory_without_persisting_it_in_active_history() -> None:
    engine = _engine_with_memory()
    assert engine.memory is not None
    engine.memory.remember("run uv sync before tests", scope=MemoryScope.PROJECT)
    request = ContextRequest(
        session=SessionRef(id="memory-session"),
        agent=AgentRef(name="test"),
        prompt="how should I run the tests?",
        task=TaskSnapshot(plan=PlanState()),
        runtime=RuntimeContextSnapshot(instructions="be helpful"),
        history=(ModelRequest(parts=[UserPromptPart(content="hello")]),),
    )

    envelope = await engine.prepare(request, _noop)
    visible = "\n".join(
        str(getattr(part, "content", ""))
        for message in envelope.messages
        for part in getattr(message, "parts", ())
    )
    assert "run uv sync before tests" in visible
    assert envelope.budget is not None
    assert any(zone.zone.value == "memory_index" for zone in envelope.budget.zones)

    transition = await engine.commit(
        ContextCommit(
            session=request.session,
            envelope_fingerprint=envelope.fingerprint,
            new_messages=(ModelResponse(parts=[TextPart(content="done")]),),
        ),
        _noop,
    )
    canonical = "\n".join(
        str(getattr(part, "content", ""))
        for message in transition.active_history
        for part in getattr(message, "parts", ())
    )
    assert "run uv sync before tests" not in canonical
