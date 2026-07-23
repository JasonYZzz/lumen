import json
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from lumen.context import CompactionRecord, ContextSummary
from lumen.events import RunStarted, TextDelta, TimelineEventRecord
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.sessions import SCHEMA_VERSION, SessionCorruptError, SessionRepository


def test_session_round_trip_preserves_model_messages(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hello")])]

    repository.append_turn(
        session.id,
        user_input="hello",
        messages=messages,
        approvals=[],
        usage={"input_tokens": 1},
        status="completed",
    )
    loaded = repository.load(session.id)

    assert loaded.metadata.id == session.id
    assert len(loaded.history) == 1
    assert isinstance(loaded.history[0], ModelRequest)
    assert loaded.full_history == loaded.history
    assert loaded.turns[0].user_input == "hello"
    assert loaded.plan == PlanState()
    assert SCHEMA_VERSION == 4


def test_session_v4_round_trip_preserves_timeline_events(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    records = [
        TimelineEventRecord.from_event(RunStarted("literal @README.md"), sequence=1),
        TimelineEventRecord.from_event(TextDelta("answer"), sequence=2),
    ]

    repository.append_turn(
        session.id,
        user_input="literal @README.md",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
        timeline_events=records,
    )

    loaded = repository.load(session.id).turns[0]
    assert [record.to_event() for record in loaded.timeline_events] == [
        RunStarted("literal @README.md"),
        TextDelta("answer"),
    ]


def test_failed_turn_round_trip_preserves_partial_audit_without_model_history(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")

    repository.append_turn(
        session.id,
        user_input="do work",
        messages=[],
        approvals=[{"name": "write_file", "approved": True}],
        usage={"input_tokens": 12},
        status="failed",
        diagnostics=[{"category": "provider"}],
        error_message="connection lost",
        partial_text="half an answer",
        retryable=True,
    )
    loaded = repository.load(session.id)

    turn = loaded.turns[0]
    assert turn.error_message == "connection lost"
    assert turn.partial_text == "half an answer"
    assert turn.retryable is True
    assert turn.diagnostics == [{"category": "provider"}]
    assert loaded.history == []


def test_turn_pages_are_chronological_and_cursor_based(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    for number in range(5):
        repository.append_turn(
            session.id,
            user_input=f"q{number}",
            messages=[],
            approvals=[],
            usage={},
            status="failed",
        )

    newest = repository.load_turn_page(session.id, limit=2)
    older = repository.load_turn_page(session.id, before=newest.next_before, limit=2)

    assert [turn.user_input for turn in newest.turns] == ["q3", "q4"]
    assert [turn.user_input for turn in older.turns] == ["q1", "q2"]
    assert newest.next_before == 3
    assert older.next_before == 1


def test_session_load_rejects_partial_json_line(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    session.path.write_text(session.path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")

    with pytest.raises(SessionCorruptError, match="line 2"):
        repository.load(session.id)


def test_session_persists_plan_diagnostics_and_compaction(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    plan = PlanState(
        steps=[PlanStep(id="one", title="One", status=StepStatus.COMPLETED)],
        revision=1,
    )
    diagnostics = [{"category": "timeout", "message": "tool slow", "retryable": True}]
    summary = ContextSummary(
        goals=["x"],
        constraints=[],
        completed=[],
        current_plan=[],
        important_files=[],
        key_facts=[],
        failures_and_approvals=[],
        outstanding=[],
    )
    record = CompactionRecord(summary, [], 5, {"input_tokens": 7})
    new_messages = [ModelResponse(parts=[TextPart(content="ok")])]

    repository.append_turn(
        session.id,
        user_input="task",
        messages=new_messages,
        approvals=[],
        usage={},
        status="completed",
        plan=plan,
        diagnostics=diagnostics,
        compaction=record,
    )
    loaded = repository.load(session.id)

    assert loaded.plan == plan
    assert loaded.turns[0].diagnostics == diagnostics
    assert loaded.turns[0].compaction is not None
    assert loaded.turns[0].compaction["source_message_count"] == 5


def test_session_restores_latest_compacted_active_history(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    old_messages = [ModelRequest(parts=[UserPromptPart(content="old question")])]
    compacted = [ModelRequest(parts=[SystemPromptPart(content="Prior summary")])]
    new_messages = [
        ModelRequest(parts=[UserPromptPart(content="new question")]),
        ModelResponse(parts=[TextPart(content="new answer")]),
    ]
    plan = PlanState(steps=[PlanStep(id="one", title="One", status=StepStatus.COMPLETED)])
    summary = ContextSummary(
        goals=["finish work"],
        constraints=[],
        completed=["one"],
        current_plan=["one: completed"],
        important_files=[],
        key_facts=[],
        failures_and_approvals=[],
        outstanding=[],
    )
    record = CompactionRecord(summary, list(compacted), len(old_messages), {})
    repository.append_turn(
        session.id,
        user_input="old question",
        messages=old_messages,
        approvals=[],
        usage={},
        status="completed",
        plan=PlanState(),
        diagnostics=[],
        compaction=None,
    )
    repository.append_turn(
        session.id,
        user_input="new question",
        messages=new_messages,
        approvals=[],
        usage={},
        status="completed",
        plan=plan,
        diagnostics=[],
        compaction=record,
    )
    loaded = repository.load(session.id)
    assert loaded.plan == plan
    assert loaded.history == record.active_history + new_messages
    assert loaded.full_history == old_messages + new_messages


def test_session_restores_latest_compaction_summary(tmp_path: Path) -> None:
    """load() materialises the last turn's ContextSummary so /resume can
    restore it for iterative compaction — isolated to THIS session."""
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    messages = [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(parts=[TextPart(content="a")]),
    ]
    summary = ContextSummary(goals=["the goal"], key_facts=["a fact"])
    record = CompactionRecord(summary, list(messages), len(messages), {})
    repository.append_turn(
        session.id,
        user_input="q",
        messages=messages,
        approvals=[],
        usage={},
        status="completed",
        plan=PlanState(),
        diagnostics=[],
        compaction=record,
    )
    loaded = repository.load(session.id)
    # The summary is restored as a real ContextSummary, not raw dict.
    assert isinstance(loaded.latest_compaction_summary, ContextSummary)
    assert loaded.latest_compaction_summary.goals == ["the goal"]
    assert loaded.latest_compaction_summary.key_facts == ["a fact"]


def test_session_without_compaction_has_none_summary(tmp_path: Path) -> None:
    """A session that never compacted restores None, not a stale value."""
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    messages = [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(parts=[TextPart(content="a")]),
    ]
    repository.append_turn(
        session.id,
        user_input="q",
        messages=messages,
        approvals=[],
        usage={},
        status="completed",
        plan=PlanState(),
        diagnostics=[],
        compaction=None,
    )
    loaded = repository.load(session.id)
    assert loaded.latest_compaction_summary is None


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_legacy_session_loads_without_rewriting(tmp_path: Path, schema_version: int) -> None:
    session_id = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session",
                "schema_version": schema_version,
                "id": session_id,
                "agent_name": "legacy",
                "model_id": "test",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "turn",
                "user_input": "q",
                "messages": [{"parts": [{"part_kind": "user-prompt", "content": "q"}], "kind": "request"}],
                "approvals": [],
                "usage": {},
                "status": "completed",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = SessionRepository(tmp_path).load(session_id)
    assert loaded.plan == PlanState()
    assert len(loaded.history) == 1
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["schema_version"] == schema_version


def test_session_rejects_unknown_schema_version(tmp_path: Path) -> None:
    session_id = "00000000-0000-0000-0000-000000000002"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "session",
                "schema_version": 7,
                "id": session_id,
                "agent_name": "x",
                "model_id": "test",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionCorruptError, match="unsupported session schema"):
        SessionRepository(tmp_path).load(session_id)
