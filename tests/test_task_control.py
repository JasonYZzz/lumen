from __future__ import annotations

import pytest

from lumen.events import (
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunEvent,
)
from lumen.plan import PlanState, PlanStep, PlanStepInput, StepStatus
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
    with pytest.raises(ValueError, match="completed step"):
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


async def test_update_step_rejects_second_active_step() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="a", title="A"), PlanStepInput(id="b", title="B")])
    await controller.update_step("a", StepStatus.IN_PROGRESS)
    with pytest.raises(ValueError, match="in_progress"):
        await controller.update_step("b", StepStatus.IN_PROGRESS)


async def test_set_plan_increments_revision() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="one", title="One")])
    await controller.set_plan([PlanStepInput(id="two", title="Two")])
    snapshot = controller.snapshot()
    assert snapshot.revision == 2
    assert [step.id for step in snapshot.steps] == ["two"]


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


def test_plan_state_rejects_multiple_active_steps_at_model_seam() -> None:
    with pytest.raises(ValueError, match="at most one in_progress"):
        PlanState(
            steps=[
                PlanStep(id="one", title="One", status=StepStatus.IN_PROGRESS),
                PlanStep(id="two", title="Two", status=StepStatus.IN_PROGRESS),
            ]
        )


def test_progress_reported_event_round_trips() -> None:
    event = ProgressReported(summary="hi", next_action="go")
    assert event.summary == "hi"
    assert event.next_action == "go"
