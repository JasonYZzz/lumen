"""Public-behaviour tests for M6 two-stage automatic memory."""

from __future__ import annotations

from pathlib import Path

from lumen.context.memory import InMemoryMemoryRepository, MemoryManager, MemoryScope
from lumen.context.memory.extraction import (
    ExtractionProvenance,
    InMemoryMemoryWorkQueue,
    RawFact,
    SessionExtractionSource,
    SQLiteMemoryWorkQueue,
    extract_candidates,
)


def test_extraction_uses_host_provenance_not_model_claims() -> None:
    provenance = ExtractionProvenance(
        user_event_ids=frozenset({"user:1"}),
        external_event_ids=frozenset({"mcp:1"}),
        locally_verified_event_ids=frozenset({"local:1"}),
    )
    facts = [
        RawFact(content="external only", source_event_ids=("mcp:1",)),
        RawFact(content="locally verified", source_event_ids=("local:1",)),
        RawFact(content="user preference", source_event_ids=("user:1",)),
        RawFact(content="fabricated evidence", source_event_ids=("invented:1",)),
        RawFact(content="assistant laundering", source_event_ids=("assistant:1",)),
        RawFact(content="no evidence"),
        RawFact(content="TOKEN=sk-1234567890abcdefghijklmnop", source_event_ids=("local:1",)),
    ]

    candidates = extract_candidates(facts, session_id="s1", provenance=provenance)

    assert [candidate.content for candidate in candidates] == ["locally verified", "user preference"]


def test_extraction_drops_personally_identifying_information() -> None:
    provenance = ExtractionProvenance(locally_verified_event_ids=frozenset({"local:1"}))
    facts = [
        RawFact(content="contact alice@example.com", source_event_ids=("local:1",)),
        RawFact(content="call 13800138000", source_event_ids=("local:1",)),
    ]

    assert extract_candidates(facts, session_id="s1", provenance=provenance) == []


def test_sqlite_work_queue_recovers_jobs_and_serializes_scope_leases(tmp_path: Path) -> None:
    path = tmp_path / "memory-work.sqlite3"
    first = SQLiteMemoryWorkQueue(path)
    job = first.enqueue("session-1", "digest-1")
    assert job is not None
    assert first.claim(lease_seconds=0.0) == job
    first.fail(job.id, "temporary", base_delay_seconds=0.0, max_attempts=3)
    first.close()

    second = SQLiteMemoryWorkQueue(path)
    recovered = second.claim(lease_seconds=30.0)
    assert recovered is not None
    assert recovered.id == job.id
    assert second.acquire_scope(MemoryScope.PROJECT, "worker-b", lease_seconds=30.0)

    third = SQLiteMemoryWorkQueue(path)
    assert not third.acquire_scope(MemoryScope.PROJECT, "worker-c", lease_seconds=30.0)
    second.release_scope(MemoryScope.PROJECT, "worker-b")
    assert third.acquire_scope(MemoryScope.PROJECT, "worker-c", lease_seconds=30.0)
    second.close()
    third.close()


async def test_manager_learn_switch_controls_calls_and_replay_is_idempotent() -> None:
    calls = 0
    source = SessionExtractionSource(
        session_id="session-1",
        transcript_text="four useful turns",
        turn_count=4,
        has_stable_result=True,
        provenance=ExtractionProvenance(
            locally_verified_event_ids=frozenset({"local:1"}),
        ),
    )

    async def extract(_source: SessionExtractionSource) -> list[RawFact]:
        nonlocal calls
        calls += 1
        return [RawFact(content="use uv run pytest", source_event_ids=("local:1",))]

    manager = MemoryManager(
        InMemoryMemoryRepository(),
        project_id="repo-a",
        learn=False,
        extractor=extract,
        source_loader=lambda _session_id: source,
        work_queue=InMemoryMemoryWorkQueue(),
    )
    assert manager.schedule_session("session-1", idle_seconds=0) is False
    await manager.drain_pending()
    assert calls == 0

    manager.learn = True
    assert manager.schedule_session("session-1", idle_seconds=0) is True
    await manager.wait_for_idle()
    assert calls == 1
    learned = manager.recall("pytest")
    assert [record.content for record in learned] == ["use uv run pytest"]
    assert learned[0].valid_until is not None

    # The same session digest has already completed; replay queues no new work.
    assert manager.schedule_session("session-1", idle_seconds=0) is False
    await manager.drain_pending()
    assert calls == 1
    await manager.close()


async def test_incognito_disables_recall_and_learning() -> None:
    source = SessionExtractionSource(
        session_id="s",
        transcript_text="private session",
        turn_count=10,
        has_stable_result=True,
    )
    manager = MemoryManager(
        InMemoryMemoryRepository(),
        project_id="repo-a",
        learn=True,
        source_loader=lambda _session_id: source,
        work_queue=InMemoryMemoryWorkQueue(),
    )
    manager.remember("visible before incognito")
    manager.set_incognito(True)

    assert manager.recall("visible") == []
    assert manager.schedule_session("s", idle_seconds=0) is False
    await manager.close()


async def test_incognito_cancels_already_queued_learning() -> None:
    source = SessionExtractionSource(
        session_id="s",
        transcript_text="private session with stable results",
        turn_count=10,
        has_stable_result=True,
    )
    extracted = False

    async def extract(_source: SessionExtractionSource) -> list[RawFact]:
        nonlocal extracted
        extracted = True
        return [RawFact(content="must not survive incognito")]

    manager = MemoryManager(
        InMemoryMemoryRepository(),
        project_id="repo-a",
        learn=True,
        extractor=extract,
        source_loader=lambda _session_id: source,
        work_queue=InMemoryMemoryWorkQueue(),
    )
    assert manager.schedule_session("s", idle_seconds=3600)

    manager.set_incognito(True)
    manager.set_incognito(False)
    await manager.drain_pending()

    assert extracted is False
    assert manager.learning_status()["queue"] == {"cancelled": 1}
    await manager.close()
