from __future__ import annotations

import pytest

from lumen.events import (
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunEvent,
)
from lumen.plan import (
    AcceptanceCriterion,
    EvidenceKind,
    EvidenceReceipt,
    PlanState,
    PlanStep,
    PlanStepInput,
    StepStatus,
)
from lumen.task_control import TaskController


async def test_controller_creates_and_updates_one_active_step() -> None:
    events: list[RunEvent] = []
    controller = TaskController()
    controller.start(PlanState(), events.append)
    await controller.set_plan(
        [
            PlanStepInput(id="inspect", title="Inspect project"),
            PlanStepInput(id="test", title="Run tests"),
        ]
    )
    await controller.update_step("inspect", StepStatus.IN_PROGRESS, "Reading config")
    assert controller.snapshot().steps[0].status is StepStatus.IN_PROGRESS
    assert isinstance(events[0], PlanCreated)
    assert isinstance(events[1], PlanUpdated)


async def test_controller_rejects_completed_to_pending() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="one", title="One")])
    await controller.update_step("one", StepStatus.IN_PROGRESS)
    await controller.update_step("one", StepStatus.COMPLETED)
    with pytest.raises(ValueError, match="finished step"):
        await controller.update_step("one", StepStatus.PENDING)


async def test_progress_is_public_and_bounded() -> None:
    events: list[RunEvent] = []
    controller = TaskController()
    controller.start(PlanState(), events.append)
    result = await controller.report_progress("Found the config entry.", "Inspect tests")
    assert result == "Progress reported."
    assert events == [ProgressReported("Found the config entry.", "Inspect tests")]


async def test_report_progress_rejects_empty_summary() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    with pytest.raises(ValueError, match="empty"):
        await controller.report_progress("   ")


async def test_report_progress_rejects_oversize_summary() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    with pytest.raises(ValueError, match="800"):
        await controller.report_progress("x" * 801)


async def test_set_plan_rejects_duplicate_ids() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    with pytest.raises(ValueError, match="duplicate"):
        await controller.set_plan(
            [PlanStepInput(id="dup", title="One"), PlanStepInput(id="dup", title="Two")]
        )


async def test_update_step_allows_independent_parallel_steps() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="a", title="A"), PlanStepInput(id="b", title="B")])
    await controller.update_step("a", StepStatus.IN_PROGRESS)
    await controller.update_step("b", StepStatus.IN_PROGRESS)
    assert [step.status for step in controller.snapshot().steps] == [
        StepStatus.IN_PROGRESS,
        StepStatus.IN_PROGRESS,
    ]


async def test_set_plan_increments_revision() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="one", title="One")])
    await controller.set_plan([PlanStepInput(id="two", title="Two")])
    snapshot = controller.snapshot()
    assert snapshot.revision == 2
    assert [step.id for step in snapshot.steps] == ["two"]


async def test_revising_plan_preserves_completed_steps_and_evidence() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    first = PlanStepInput(id="read", title="Read source")
    await controller.set_plan([first], goal="Review")
    controller.record_evidence(EvidenceReceipt(
        id="receipt", kind=EvidenceKind.TOOL, source_id="read-call", summary="Read source", passed=True,
    ))
    await controller.attach_executor_evidence("read", "receipt")
    await controller.update_step("read", StepStatus.COMPLETED, note="Checked", owner="root")
    before = controller.snapshot()
    await controller.set_plan([first, PlanStepInput(id="review", title="Write review")])
    after = controller.snapshot()
    assert after.steps[0] == before.steps[0]
    assert after.evidence == before.evidence
    assert after.goal == "Review"
    assert after.steps[1].status is StepStatus.PENDING
    assert after.approved_revision is None
    await controller.set_plan([first, PlanStepInput(id="review", title="Write review")])
    assert controller.snapshot() == after


async def test_changed_definition_invalidates_dependent_progress_but_new_goal_resets_all() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    steps = [PlanStepInput(id="a", title="A"), PlanStepInput(id="b", title="B", depends_on=["a"])]
    await controller.set_plan(steps, goal="First")
    await controller.update_step("a", StepStatus.COMPLETED)
    await controller.update_step("b", StepStatus.COMPLETED)
    await controller.set_plan([PlanStepInput(id="a", title="Changed"), steps[1]])
    assert all(step.status is StepStatus.PENDING for step in controller.snapshot().steps)
    await controller.update_step("a", StepStatus.COMPLETED)
    await controller.set_plan([PlanStepInput(id="a", title="Changed")], goal="New task")
    assert controller.snapshot().steps[0].status is StepStatus.PENDING


async def test_plan_ownership_is_run_local_and_not_claimed_by_public_progress() -> None:
    controller = TaskController()
    plan = PlanState(steps=[PlanStep(id="a", title="A")])
    controller.start(plan, lambda _event: None)
    await controller.report_progress("Answering an unrelated question")
    assert not controller.plan_updated
    await controller.update_step("a", StepStatus.IN_PROGRESS)
    assert controller.plan_updated
    controller.start(controller.snapshot(), lambda _event: None)
    assert not controller.plan_updated


async def test_update_step_rejects_unknown_id() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="known", title="Known")])
    with pytest.raises(ValueError, match="unknown step"):
        await controller.update_step("missing", StepStatus.IN_PROGRESS)


def test_plan_step_input_pattern_rejects_invalid_ids() -> None:
    with pytest.raises(ValueError):
        PlanStepInput(id="bad id", title="x")
    with pytest.raises(ValueError):
        PlanStepInput(id="", title="x")
    # leading digit is allowed by the plan regex
    PlanStepInput(id="1abc", title="x")


def test_plan_state_is_strict() -> None:
    with pytest.raises(ValueError):
        PlanStep(id="ok", title="t", status=StepStatus.PENDING, extra="nope")  # type: ignore[call-arg]


def test_plan_state_allows_parallel_steps_and_rejects_cycles() -> None:
    state = PlanState(
        steps=[
            PlanStep(id="one", title="One", status=StepStatus.IN_PROGRESS),
            PlanStep(id="two", title="Two", status=StepStatus.IN_PROGRESS),
        ]
    )
    assert len(state.steps) == 2
    with pytest.raises(ValueError, match="cycle"):
        PlanState(
            steps=[
                PlanStep(id="one", title="One", depends_on=["two"]),
                PlanStep(id="two", title="Two", depends_on=["one"]),
            ]
        )


async def test_dependencies_and_passing_evidence_gate_completion() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan(
        [
            PlanStepInput(
                id="build",
                title="Build",
                acceptance_criteria=[AcceptanceCriterion(id="tests", description="Tests pass")],
            ),
            PlanStepInput(id="ship", title="Ship", depends_on=["build"]),
        ],
        goal="Ship safely",
    )
    with pytest.raises(ValueError, match="incomplete dependencies"):
        await controller.update_step("ship", StepStatus.IN_PROGRESS)
    await controller.update_step("build", StepStatus.IN_PROGRESS)
    with pytest.raises(ValueError, match="no passing evidence"):
        await controller.update_step("build", StepStatus.COMPLETED)
    controller.record_evidence(
        EvidenceReceipt(
            id="receipt-tests",
            kind=EvidenceKind.COMMAND,
            source_id="call-tests",
            summary="tests passed",
            passed=True,
        )
    )
    await controller.link_evidence("build", "receipt-tests", ["tests"])
    await controller.update_step("build", StepStatus.COMPLETED)
    await controller.update_step("ship", StepStatus.IN_PROGRESS)


async def test_state_updates_do_not_invalidate_structural_revision() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="one", title="One")], goal="Goal")
    revision = controller.snapshot().revision
    state_version = controller.snapshot().state_version
    await controller.update_step("one", StepStatus.IN_PROGRESS)
    snapshot = controller.snapshot()
    assert snapshot.revision == revision
    assert snapshot.state_version > state_version


def test_progress_reported_event_round_trips() -> None:
    event = ProgressReported(summary="hi", next_action="go")
    assert event.summary == "hi"
    assert event.next_action == "go"
