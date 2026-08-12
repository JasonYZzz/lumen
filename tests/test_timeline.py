from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from lumen.events import (
    CommentaryDelta,
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunFailed,
    RunStarted,
    TextDelta,
    TextRetracted,
    TimelineEventRecord,
    ToolCallFinished,
    ToolCallStarted,
    WorkProductChanged,
)
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.sessions import SessionRepository
from lumen.timeline import (
    InMemoryTimelineAdapter,
    RepositoryTimelineAdapter,
    TimelineKind,
    TimelineStore,
)


def test_timeline_window_is_bounded_after_many_events() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())

    for index in range(1_000):
        store.apply(ProgressReported(summary=f"step {index}"))

    window = store.window(limit=200)
    assert len(window) == 200
    assert window[0].text == "step 800"
    assert window[-1].text == "step 999"


def test_timeline_coalesces_stream_and_tool_lifecycle() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())
    store.apply(RunStarted("hello"))
    store.apply(TextDelta("one"))
    store.apply(TextDelta(" two"))
    store.apply(
        ToolCallStarted(
            call_id="call-1",
            name="read_file",
            args={"path": "README.md"},
            origin="builtin",
            risk="read",
        )
    )
    store.apply(
        ToolCallFinished(
            call_id="call-1",
            name="read_file",
            result="the complete result",
            preview="short result",
            is_error=False,
        )
    )

    items = store.window()
    assert [item.kind for item in items] == [
        TimelineKind.USER,
        TimelineKind.ASSISTANT,
        TimelineKind.TOOL,
    ]
    assert items[1].text == "one two"
    assert items[2].result == "the complete result"
    assert items[2].preview == "short result"
    assert items[2].status == "ok"


def test_timeline_renders_work_product_lifecycle_event() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())
    event = WorkProductChanged(
        phase="verified",
        work_product_id="work:1",
        resource="report.md",
        effect_id="effect:1",
        status="verified",
        summary="target changed; non-target content unchanged",
    )
    store.apply(event)

    item = store.window()[0]
    assert item.kind is TimelineKind.WORK_PRODUCT
    assert item.status == "verified"
    assert "report.md" in item.text
    assert TimelineEventRecord.from_event(event, sequence=1).to_event() == event


def test_timeline_keeps_each_plan_with_its_user_turn() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())
    first = PlanState(steps=[PlanStep(id="inspect", title="Inspect")])
    completed = PlanState(
        revision=1,
        steps=[PlanStep(id="inspect", title="Inspect", status=StepStatus.COMPLETED)],
    )
    second = PlanState(steps=[PlanStep(id="test", title="Test")])

    store.apply(RunStarted("first request"))
    store.apply(PlanCreated(first))
    store.apply(PlanUpdated(completed))
    store.apply(TextDelta("first answer"))
    store.apply(RunStarted("second request"))
    store.apply(PlanCreated(second))

    items = store.window()
    assert [item.kind for item in items] == [
        TimelineKind.USER,
        TimelineKind.PLAN,
        TimelineKind.ASSISTANT,
        TimelineKind.USER,
        TimelineKind.PLAN,
    ]
    assert items[1].plan == completed.model_dump(mode="json")
    assert items[4].plan == second.model_dump(mode="json")


def test_timeline_hides_control_tools_behind_plan_and_progress_components() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())
    plan = PlanState(steps=[PlanStep(id="inspect", title="Inspect")])

    store.apply(RunStarted("inspect"))
    store.apply(ToolCallStarted("control-1", "set_plan", {}, "control", "read"))
    store.apply(PlanCreated(plan))
    store.apply(ToolCallFinished("control-1", "set_plan", "Plan updated.", False))
    store.apply(ProgressReported(summary="**Step 1:** inspect"))

    items = store.window()
    assert [item.kind for item in items] == [
        TimelineKind.USER,
        TimelineKind.PLAN,
        TimelineKind.PROGRESS,
    ]
    assert all(item.tool_name != "set_plan" for item in items)


def test_timeline_reclassifies_provisional_stream_as_commentary() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())
    store.apply(RunStarted("inspect"))
    store.apply(TextDelta("Inspect"))
    store.apply(TextDelta("ing."))
    store.apply(TextRetracted(len("Inspecting.")))
    store.apply(CommentaryDelta("Inspect"))
    store.apply(CommentaryDelta("ing."))
    store.apply(
        ToolCallStarted(
            call_id="call-1",
            name="read_file",
            args={"path": "README.md"},
            origin="builtin",
            risk="read",
        )
    )
    store.apply(TextDelta("Done."))

    items = store.window()
    assert [item.kind for item in items] == [
        TimelineKind.USER,
        TimelineKind.COMMENTARY,
        TimelineKind.TOOL,
        TimelineKind.ASSISTANT,
    ]
    assert items[1].text == "Inspecting."
    assert items[-1].text == "Done."


def test_repository_adapter_pages_failed_turns_into_visible_audit_items(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="agent", model_id="test")
    for index in range(3):
        repository.append_turn(
            session.id,
            user_input=f"request {index}",
            messages=(
                [
                    ModelRequest(parts=[UserPromptPart(content=f"request {index}")]),
                    ModelResponse(parts=[TextPart(content=f"answer {index}")]),
                ]
                if index < 2
                else []
            ),
            approvals=[],
            usage={},
            status="completed" if index < 2 else "failed",
            error_message="provider unavailable" if index == 2 else None,
            partial_text="partial answer" if index == 2 else None,
        )

    store = TimelineStore(RepositoryTimelineAdapter(repository, session.id))
    newest = store.load_older(limit=1)
    assert newest.next_cursor == 2
    assert [item.kind for item in newest.items] == [
        TimelineKind.USER,
        TimelineKind.ASSISTANT,
        TimelineKind.ERROR,
    ]
    assert newest.items[1].text == "partial answer"
    assert newest.items[2].text == "provider unavailable"

    older = store.load_older(newest.next_cursor, limit=2)
    assert older.next_cursor is None
    assert store.window()[0].text == "request 0"


def test_run_failure_is_visible_without_becoming_assistant_history() -> None:
    store = TimelineStore(InMemoryTimelineAdapter())
    store.apply(RunStarted("request"))
    store.apply(TextDelta("partial"))
    store.apply(RunFailed("boom"))

    assert store.window()[-1].kind is TimelineKind.ERROR
    assert store.window()[-1].text == "boom"


def test_repository_adapter_restores_full_tool_details(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="agent", model_id="test")
    repository.append_turn(
        session.id,
        user_input="read it",
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_file",
                        args={"path": "README.md"},
                        tool_call_id="call-1",
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="read_file",
                        content="complete file contents",
                        tool_call_id="call-1",
                    )
                ]
            ),
        ],
        approvals=[],
        usage={},
        status="completed",
    )

    store = TimelineStore(RepositoryTimelineAdapter(repository, session.id))
    page = store.load_older(limit=20)
    tool = next(item for item in page.items if item.kind is TimelineKind.TOOL)

    assert tool.args == {"path": "README.md"}
    assert tool.result == "complete file contents"
    assert tool.preview == "complete file contents"


def test_repository_adapter_replays_v4_events_in_original_order(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="agent", model_id="test")
    events = [
        RunStarted("literal input"),
        TextDelta("before tool"),
        ToolCallStarted("call-1", "read_file", {"path": "README.md"}, "builtin", "read"),
        ToolCallFinished("call-1", "read_file", "contents", False),
        TextDelta("after tool"),
    ]
    repository.append_turn(
        session.id,
        user_input="literal input",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
        timeline_events=[
            TimelineEventRecord.from_event(event, sequence=index) for index, event in enumerate(events, 1)
        ],
    )

    store = TimelineStore(RepositoryTimelineAdapter(repository, session.id))
    page = store.load_older(limit=20)

    assert [item.kind for item in page.items] == [
        TimelineKind.USER,
        TimelineKind.ASSISTANT,
        TimelineKind.TOOL,
        TimelineKind.ASSISTANT,
    ]
    assert page.items[1].text == "before tool"
    assert page.items[3].text == "after tool"


def test_repository_adapter_does_not_attach_inherited_plan_to_eventful_turn(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="agent", model_id="test")
    plan = PlanState(steps=[PlanStep(id="inspect", title="Inspect")])
    repository.append_turn(
        session.id,
        user_input="inspect this",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
        plan=plan,
        timeline_events=[
            TimelineEventRecord.from_event(RunStarted("inspect this"), sequence=1),
            TimelineEventRecord.from_event(TextDelta("done"), sequence=2),
        ],
    )

    store = TimelineStore(RepositoryTimelineAdapter(repository, session.id))
    page = store.load_older(limit=20)

    assert [item.kind for item in page.items] == [
        TimelineKind.USER,
        TimelineKind.ASSISTANT,
    ]


def test_repository_adapter_keeps_explicit_plan_on_original_turn_after_refresh(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="agent", model_id="test")
    plan = PlanState(steps=[PlanStep(id="report", title="Write report")])
    first_events = [RunStarted("write a report"), PlanCreated(plan), TextDelta("done")]
    second_events = [RunStarted("what is its ranking?"), TextDelta("ranked answer")]
    for prompt, events in (
        ("write a report", first_events),
        ("what is its ranking?", second_events),
    ):
        repository.append_turn(
            session.id,
            user_input=prompt,
            messages=[],
            approvals=[],
            usage={},
            status="completed",
            plan=plan,
            timeline_events=[
                TimelineEventRecord.from_event(event, sequence=index)
                for index, event in enumerate(events, 1)
            ],
        )

    store = TimelineStore(RepositoryTimelineAdapter(repository, session.id))
    page = store.load_older(limit=20)

    assert [item.kind for item in page.items] == [
        TimelineKind.USER,
        TimelineKind.PLAN,
        TimelineKind.ASSISTANT,
        TimelineKind.USER,
        TimelineKind.ASSISTANT,
    ]
    assert sum(item.kind is TimelineKind.PLAN for item in page.items) == 1
