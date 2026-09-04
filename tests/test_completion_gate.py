from __future__ import annotations

import pytest

from lumen.completion import CompletionBlocker, CompletionGate, CompletionPolicy
from lumen.plan import PlanState, PlanStep, StepStatus


def test_completion_assessment_preserves_operator_recovery_ownership() -> None:
    blocker = CompletionBlocker("remote outcome unresolved", model_recoverable=False)
    gate = CompletionGate(lambda _: [blocker])
    assert gate.assess(session_id="s", plan=PlanState(), policy=CompletionPolicy()) == [blocker]
    assert gate.evaluate(session_id="s", plan=PlanState(), policy=CompletionPolicy()) == [blocker.message]


@pytest.mark.parametrize("status", [StepStatus.PENDING, StepStatus.IN_PROGRESS])
def test_default_mode_requires_current_plan_progress(status: StepStatus) -> None:
    plan = PlanState(steps=[PlanStep(id="a", title="A", status=status)])
    gate = CompletionGate()
    assert gate.evaluate(session_id="s", plan=plan, policy=CompletionPolicy(), plan_updated=True)
    assert not gate.evaluate(session_id="s", plan=plan, policy=CompletionPolicy(), plan_updated=False)


@pytest.mark.parametrize("status", [StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.BLOCKED])
def test_default_mode_allows_explicit_outcomes_without_faking_success(status: StepStatus) -> None:
    plan = PlanState(steps=[PlanStep(id="a", title="A", status=status, note="Explained")])
    assert not CompletionGate().evaluate(
        session_id="s", plan=plan, policy=CompletionPolicy(), plan_updated=True,
    )


def test_plan_mode_keeps_proposed_work_pending_and_external_gates_active() -> None:
    plan = PlanState(goal="Review", steps=[PlanStep(id="a", title="A")])
    assert CompletionGate(lambda _: ["external issue"]).evaluate(
        session_id="s", plan=plan, policy=CompletionPolicy(require_reviewable_plan=True), plan_updated=True,
    ) == ["external issue"]


def test_approved_execution_still_rejects_blocked_steps_without_new_plan_events() -> None:
    plan = PlanState(revision=1, approved_revision=1, steps=[
        PlanStep(id="a", title="A", status=StepStatus.BLOCKED),
    ])
    assert CompletionGate().evaluate(session_id="s", plan=plan, policy=CompletionPolicy()) == [
        "step a is blocked",
    ]
