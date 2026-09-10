import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from lumen.agents.types import (
    AgentConfigSnapshot,
    AgentEvent,
    AgentEventKind,
    AgentMessage,
    AgentResult,
    AgentStatus,
    AgentThreadRef,
    AgentThreadState,
)
from lumen.collaboration import CollaborationMode, PlanReviewStatus, SessionSettingsState
from lumen.context import (
    CompactionCheckpointV1,
    CompactionCheckpointV2,
    CompactionRecord,
    ContextSummary,
    ModelInputManifest,
    ProviderRequestReceipt,
    ReplayEligibility,
    RollingContextState,
    SessionContextState,
    TranscriptCursor,
)
from lumen.events import RunStarted, TextDelta, TimelineEventRecord
from lumen.live import LiveConnectionState, LiveSessionRef, LiveSessionState
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.sessions import (
    SCHEMA_VERSION,
    SessionCatalogState,
    SessionCorruptError,
    SessionRepository,
)
from lumen.tools.spec import EffectKind
from lumen.work_products import EffectReceipt, EffectStatus, SessionWorkState


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
    assert SCHEMA_VERSION == 11


@pytest.mark.parametrize("turn_index", [0, 1, 2])
def test_message_edit_fork_keeps_only_prefix_without_rewriting_source(
    tmp_path: Path, turn_index: int,
) -> None:
    repository = SessionRepository(tmp_path)
    source = repository.create(agent_name="test", model_id="test")
    for index in range(3):
        repository.append_turn(
            source.id, user_input=f"question-{index}", approvals=[], usage={}, status="completed",
            messages=[ModelRequest(parts=[UserPromptPart(content=f"question-{index}")]),
                      ModelResponse(parts=[TextPart(content=f"answer-{index}")])],
        )
    repository.append_session_settings(source.id, SessionSettingsState(
        collaboration_mode=CollaborationMode.PLAN,
        plan_review_status=PlanReviewStatus.REVIEW_PENDING,
    ))
    before = source.path.read_bytes()
    branch = repository.load(repository.fork(source.id, through_turn=turn_index, include_turn=False).id)
    assert [turn.user_input for turn in branch.turns] == [f"question-{index}" for index in range(turn_index)]
    assert len(branch.full_history) == turn_index * 2
    assert branch.settings.collaboration_mode is CollaborationMode.PLAN
    assert branch.settings.plan_review_status is PlanReviewStatus.NONE
    assert source.path.read_bytes() == before


def test_message_regeneration_rewinds_active_lineage_in_same_append_only_session(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    repository.append_session_catalog(session.id, SessionCatalogState(title="Existing title"))
    for index in range(3):
        repository.append_turn(
            session.id,
            user_input=f"question-{index}",
            approvals=[],
            usage={},
            status="completed",
            messages=[
                ModelRequest(parts=[UserPromptPart(content=f"question-{index}")]),
                ModelResponse(parts=[TextPart(content=f"stale-answer-{index}")]),
            ],
        )
    repository.append_session_settings(
        session.id,
        SessionSettingsState(
            collaboration_mode=CollaborationMode.PLAN,
            plan_review_status=PlanReviewStatus.REVIEW_PENDING,
        ),
    )
    before = session.path.read_bytes()

    repository.rewind(session.id, before_turn=1)
    repository.append_turn(
        session.id,
        user_input="question-1",
        approvals=[],
        usage={},
        status="completed",
        messages=[
            ModelRequest(parts=[UserPromptPart(content="question-1")]),
            ModelResponse(parts=[TextPart(content="fresh-answer")]),
        ],
    )
    repository.clear_projection_cache()
    loaded = repository.load(session.id)

    assert loaded.metadata.id == session.id
    assert loaded.catalog.title == "Existing title"
    assert [turn.user_input for turn in loaded.turns] == ["question-0", "question-1"]
    final_message = loaded.turns[-1].messages[-1]
    assert isinstance(final_message, ModelResponse)
    assert isinstance(final_message.parts[0], TextPart)
    assert final_message.parts[0].content == "fresh-answer"
    assert all("stale-answer-1" not in str(message) for message in loaded.full_history)
    assert all("stale-answer-2" not in str(message) for message in loaded.full_history)
    assert loaded.settings.collaboration_mode is CollaborationMode.PLAN
    assert loaded.settings.plan_review_status is PlanReviewStatus.NONE
    assert [turn.user_input for turn in repository.load_turn_page(session.id, limit=20).turns] == [
        "question-0",
        "question-1",
    ]
    after = session.path.read_bytes()
    assert after.startswith(before)
    assert b'"type":"history_rewind"' in after[len(before):]
    assert b"stale-answer-2" in after


def test_v10_session_appends_rewind_upgrade_without_rewriting_header(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    original = session.path.read_text(encoding="utf-8").replace(
        '"schema_version":11', '"schema_version":10'
    )
    session.path.write_text(original, encoding="utf-8")
    repository.append_turn(
        session.id,
        user_input="old",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
    )

    repository.rewind(session.id, before_turn=0)

    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert records[0]["schema_version"] == 10
    assert records[-2] == {
        "type": "schema_upgrade",
        "from_version": 10,
        "to_version": 11,
        "created_at": records[-2]["created_at"],
    }
    assert records[-1]["type"] == "history_rewind"
    assert repository.load(session.id).turns == []


def test_session_turn_normalizes_decimal_values_before_jsonl_append(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")

    repository.append_turn(
        session.id,
        user_input="decimal usage",
        messages=[],
        approvals=[],
        usage={"cost": Decimal("0.0125")},
        status="completed",
    )

    loaded = repository.load(session.id)
    assert loaded.turns[0].usage == {"cost": "0.0125"}


def test_running_turn_is_durable_and_terminal_record_supersedes_its_projection(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    interaction_id = "run-1"

    repository.append_turn_started(
        session.id,
        user_input="persist before running",
        interaction_id=interaction_id,
    )

    running = repository.load(session.id)
    running_page = repository.load_turn_page(session.id, limit=20)
    assert [(turn.user_input, turn.status) for turn in running.turns] == [
        ("persist before running", "running")
    ]
    assert [(turn.user_input, turn.status) for turn in running_page.turns] == [
        ("persist before running", "running")
    ]

    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="persist before running")]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    repository.append_turn(
        session.id,
        user_input="persist before running",
        messages=messages,
        approvals=[],
        usage={},
        status="completed",
        interaction_id=interaction_id,
    )

    completed = repository.load(session.id)
    completed_page = repository.load_turn_page(session.id, limit=20)
    assert [(turn.user_input, turn.status) for turn in completed.turns] == [
        ("persist before running", "completed")
    ]
    assert [(turn.user_input, turn.status) for turn in completed_page.turns] == [
        ("persist before running", "completed")
    ]
    assert completed.history == messages
    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert [record["status"] for record in records if record["type"] == "turn"] == [
        "running",
        "completed",
    ]


def test_session_catalog_is_append_only_and_uses_latest_projection(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")

    repository.append_session_catalog(
        session.id,
        SessionCatalogState(title="First title"),
    )
    repository.append_session_catalog(
        session.id,
        SessionCatalogState(title="Renamed", archived_at="2026-08-19T00:00:00+00:00"),
    )

    loaded = repository.load(session.id)
    records = [json.loads(line) for line in session.path.read_text().splitlines()]

    assert loaded.catalog.title == "Renamed"
    assert loaded.catalog.archived_at == "2026-08-19T00:00:00+00:00"
    assert [item["type"] for item in records] == [
        "session",
        "session_catalog",
        "session_catalog",
    ]


def test_deleted_session_cannot_be_resurrected_by_a_stale_catalog_writer(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    deleted = SessionCatalogState(title="Failed research", deleted_at="2026-09-04T00:00:00+00:00")
    repository.append_session_catalog(session.id, deleted)
    before = session.path.read_bytes()
    assert repository.load(session.id).catalog == deleted

    repository.append_session_catalog(session.id, SessionCatalogState(title="Late generated title"))
    after = session.path.read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)
    assert repository.load(session.id).catalog == deleted
    repository.clear_projection_cache()
    assert repository.load(session.id).catalog == deleted
    assert session.path.read_bytes() == after


def test_request_receipt_round_trip_and_projection_cache_are_rebuildable(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="acme:test")
    digest = "sha256:" + "0" * 64
    manifest = ModelInputManifest(
        session_id=session.id,
        step=1,
        route="acme:test",
        provider="acme",
        model="test",
        context_fingerprint="sha256:context",
        message_count=2,
        tool_count=2,
        instructions_digest=digest,
        message_history_digest=digest,
        tool_schema_digest=digest,
        context_sources_digest=digest,
        stable_prefix_digest=digest,
        dynamic_tail_digest=digest,
        request_fingerprint="sha256:" + "1" * 64,
        replay_eligibility=ReplayEligibility.VERIFY_ONLY,
        non_replayable_reasons=("provider_private_framing_not_captured",),
    )
    receipt = ProviderRequestReceipt(
        step=1,
        route="acme:test",
        provider="acme",
        model="test",
        instructions_tokens=11,
        messages_tokens=13,
        tools_tokens=17,
        output_reserve_tokens=19,
        total_tokens=60,
        hard_limit_tokens=100,
        visible_tools=("read_file", "search_text"),
        visible_tool_digest="sha256:tools",
        context_fingerprint="sha256:context",
        estimated=True,
        input_manifest=manifest,
    )
    repository.append_turn(
        session.id,
        user_input="inspect",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
        request_receipts=[receipt],
    )
    original = session.path.read_bytes()

    cold = repository.load(session.id)
    warm = repository.load(session.id)
    repository.clear_projection_cache()
    rebuilt = repository.load(session.id)

    assert cold == warm == rebuilt
    assert rebuilt.turns[0].request_receipts == [receipt]
    assert rebuilt.turns[0].request_receipts[0].input_manifest == manifest
    assert session.path.read_bytes() == original


def test_session_v7_round_trip_preserves_work_state_and_effects(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    effect = EffectReceipt(
        id="effect:one",
        effect_kind=EffectKind.EXECUTION,
        operation="run_command",
        status=EffectStatus.VERIFIED,
        summary="command completed",
    )

    repository.append_work_state(session.id, SessionWorkState())
    repository.append_effect(session.id, effect)

    assert repository.load(session.id).work_state.effects == (effect,)


def test_session_v8_round_trip_preserves_agent_records(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    thread = AgentThreadState(
        ref=AgentThreadRef(
            id="agent-one",
            path="/root/one",
            parent_session_id=session.id,
            root_run_id="run-one",
            agent_type="explorer",
        ),
        task="inspect",
        task_name="one",
        config=AgentConfigSnapshot(
            model_name="test",
            model_id="test",
            cwd=str(tmp_path),
        ),
        idempotency_key="sha256:" + "0" * 64,
    )
    repository.append_agent_thread(session.id, thread)
    repository.append_agent_message(
        session.id,
        AgentMessage(id="msg-one", agent_id="agent-one", sender="/root", content="context"),
    )
    repository.append_agent_event(
        session.id,
        AgentEvent(
            id="event-one",
            sequence=1,
            agent_id="agent-one",
            session_id=session.id,
            root_run_id="run-one",
            kind=AgentEventKind.COMPLETED,
            status=AgentStatus.COMPLETED,
        ),
    )
    repository.append_agent_result(
        session.id,
        AgentResult(
            agent_id="agent-one",
            status=AgentStatus.COMPLETED,
            summary="done",
        ),
    )

    loaded = repository.load(session.id).agent_state

    assert loaded.get("agent-one") is not None
    assert loaded.get("agent-one").result.summary == "done"  # type: ignore[union-attr]
    assert loaded.messages[0].content == "context"
    assert loaded.events[0].kind is AgentEventKind.COMPLETED


def test_session_fork_marks_active_agents_not_carried(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    repository.append_turn(
        session.id,
        user_input="work",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
    )
    repository.append_agent_thread(
        session.id,
        AgentThreadState(
            ref=AgentThreadRef(
                id="agent-active",
                path="/root/active",
                parent_session_id=session.id,
                root_run_id="run-one",
                agent_type="explorer",
            ),
            task="inspect",
            task_name="active",
            status=AgentStatus.RUNNING,
            config=AgentConfigSnapshot(model_name="test", model_id="test", cwd=str(tmp_path)),
            idempotency_key="sha256:" + "1" * 64,
        ),
    )

    forked = repository.fork(session.id, through_turn=0)
    copied = repository.load(forked.id).agent_state.get("agent-active")

    assert copied is not None
    assert copied.status is AgentStatus.NOT_CARRIED
    assert copied.ref.parent_session_id == forked.id


def test_session_fork_preserves_v7_work_state(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    repository.append_turn(
        session.id,
        user_input="work",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
    )
    effect = EffectReceipt(
        id="effect:forked",
        effect_kind=EffectKind.EXECUTION,
        operation="run_command",
        status=EffectStatus.VERIFIED,
        summary="command completed",
    )
    repository.append_effect(session.id, effect)

    forked = repository.fork(session.id, through_turn=0)

    assert repository.load(forked.id).work_state.effects == (effect,)


def test_historical_v6_session_upgrades_append_only_for_work_state(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    original = session.path.read_text(encoding="utf-8").replace('"schema_version":11', '"schema_version":6')
    session.path.write_text(original, encoding="utf-8")

    assert repository.load(session.id).work_state == SessionWorkState()
    repository.append_work_state(session.id, SessionWorkState())

    lines = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["schema_version"] == 6
    assert lines[1]["type"] == "schema_upgrade"
    assert lines[2]["type"] == "work_state"
    assert repository.load(session.id).work_state == SessionWorkState()


def test_historical_session_appends_v5_v6_upgrade_chain_without_rewriting_header(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    original = session.path.read_text(encoding="utf-8").replace('"schema_version":11', '"schema_version":1')
    session.path.write_text(original, encoding="utf-8")

    context_state = SessionContextState()
    settings = SessionSettingsState(collaboration_mode=CollaborationMode.PLAN)
    plan = PlanState(steps=[PlanStep(id="one", title="One")])
    repository.append_context_state(session.id, context_state)
    repository.append_session_settings(session.id, settings)
    repository.append_plan_state(session.id, plan)
    repository.append_turn(
        session.id,
        user_input="continue",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
        plan=plan,
    )

    records = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["schema_version"] == 1
    assert [
        (record["from_version"], record["to_version"])
        for record in records
        if record["type"] == "schema_upgrade"
    ] == [(1, 5), (5, 6)]
    assert [record["type"] for record in records[1:]] == [
        "schema_upgrade",
        "context_state",
        "schema_upgrade",
        "session_settings",
        "plan_state",
        "turn",
    ]

    loaded = repository.load(session.id)
    assert loaded.context_state == context_state
    assert loaded.settings == settings
    assert loaded.plan == plan
    assert repository.load_turn_page(session.id, limit=20).turns[0].user_input == "continue"


def test_session_rejects_schema_upgrade_with_stale_from_version(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    records = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    records[0]["schema_version"] = 4
    records.append(
        {
            "type": "schema_upgrade",
            "from_version": 3,
            "to_version": 5,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    session.path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(SessionCorruptError, match="invalid schema_upgrade at line 2"):
        repository.load(session.id)
    with pytest.raises(SessionCorruptError, match="invalid schema_upgrade at line 2"):
        repository.load_turn_page(session.id, limit=20)


def test_session_v6_round_trip_preserves_timeline_events(tmp_path: Path) -> None:
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


def test_session_round_trip_preserves_independent_modes_and_plan_review(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    state = SessionSettingsState(
        collaboration_mode=CollaborationMode.PLAN,
        approval_mode="auto",
        plan_review_status=PlanReviewStatus.REVIEW_PENDING,
        reviewed_revision=3,
    )

    repository.append_session_settings(session.id, state)

    assert repository.load(session.id).settings == state


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
    checkpoint = CompactionCheckpointV1(
        checkpoint_id="cp-one",
        source_start=0,
        source_end=5,
        source_digest="sha256:abc",
        created_at=datetime.now(UTC),
    )
    compacted: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(content="Prior summary")])]
    record = CompactionRecord(summary, compacted, 5, {"input_tokens": 7}, checkpoint)
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
    # V1's active-prefix count is mapped to the absolute raw transcript
    # boundary available when the record is loaded.
    assert loaded.latest_compaction_checkpoint == checkpoint.model_copy(
        update={"source_start": 0, "source_end": 0}
    )
    assert loaded.compacted_prefix_length == 1
    assert loaded.compacted_source_end == 0


def test_session_restores_latest_compacted_active_history(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    old_messages = [ModelRequest(parts=[UserPromptPart(content="old question")])]
    compacted: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(content="Prior summary")])]
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


def test_corrupt_v2_checkpoint_falls_back_to_raw_transcript(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    source: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="old question")]),
        ModelResponse(parts=[TextPart(content="old answer")]),
    ]
    summary = ContextSummary(goals=["continue safely"])
    source_payload = json.dumps(
        ModelMessagesTypeAdapter.dump_python(source, mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    last_payload = json.dumps(
        ModelMessagesTypeAdapter.dump_python([source[-1]], mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    last_digest = hashlib.sha256(b"1:" + last_payload.encode()).hexdigest()[:16]
    rolling_state = RollingContextState(goals=("continue safely",))
    checkpoint = CompactionCheckpointV2(
        checkpoint_id="cp-v2",
        source_start=0,
        source_end=2,
        source_digest=f"sha256:{hashlib.sha256(source_payload.encode()).hexdigest()}",
        created_at=datetime.now(UTC),
        source_start_cursor=TranscriptCursor(sequence=0, message_id="session-origin"),
        source_end_cursor=TranscriptCursor(sequence=2, message_id=f"msg-{last_digest}"),
        full_history_length=2,
        rolling_state=rolling_state,
        state_digest=(f"sha256:{hashlib.sha256(rolling_state.model_dump_json().encode()).hexdigest()}"),
    )
    compacted: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(content="Prior summary")])]
    repository.append_turn(
        session.id,
        user_input="old",
        messages=source,
        approvals=[],
        usage={},
        status="completed",
    )
    repository.append_turn(
        session.id,
        user_input="next",
        messages=[ModelResponse(parts=[TextPart(content="new answer")])],
        approvals=[],
        usage={},
        status="completed",
        compaction=CompactionRecord(summary, compacted, 2, {}, checkpoint),
    )
    assert repository.load(session.id).latest_compaction_checkpoint == checkpoint

    lines = session.path.read_text(encoding="utf-8").splitlines()
    changed_projection = json.loads(lines[2])
    changed_projection["compaction"]["summary"]["goals"] = ["untrusted stale projection"]
    lines[2] = json.dumps(changed_projection, ensure_ascii=False, separators=(",", ":"))
    session.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    restored = repository.load(session.id)
    assert restored.latest_compaction_summary.goals == ["continue safely"]

    damaged = json.loads(lines[2])
    damaged["compaction"]["checkpoint"]["source_digest"] = "sha256:damaged"
    lines[2] = json.dumps(damaged, ensure_ascii=False, separators=(",", ":"))
    session.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    recovered = repository.load(session.id)
    assert recovered.latest_compaction_checkpoint is None
    assert recovered.history == recovered.full_history


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


@pytest.mark.parametrize("schema_version", list(range(1, 10)))
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
                "schema_version": SCHEMA_VERSION + 1,
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


def test_v9_live_state_is_append_only_and_owned_by_session(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test-agent", model_id="test")
    state = LiveSessionState(
        ref=LiveSessionRef(id="live-one", session_id=session.id),
        model="gpt-realtime-2.1",
        voice="marin",
    )

    repository.append_live_session(session.id, state)
    repository.append_live_session(
        session.id,
        state.model_copy(update={"connection": LiveConnectionState.CLOSED}),
    )

    loaded = repository.load(session.id)
    restored = loaded.live_state.get("live-one")
    assert restored is not None
    assert restored.connection is LiveConnectionState.CLOSED
    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert [record["type"] for record in records] == [
        "session",
        "live_session",
        "live_session",
    ]
