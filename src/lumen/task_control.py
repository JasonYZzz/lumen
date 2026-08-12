"""Per-run task controller exposing public planning control tools."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from lumen.events import PlanCreated, PlanUpdated, ProgressReported, RunEvent
from lumen.plan import (
    EvidenceReceipt,
    PlanLifecycle,
    PlanState,
    PlanStep,
    PlanStepInput,
    StepStatus,
)

EventSink = Callable[[RunEvent], None] | Callable[[RunEvent], Awaitable[None]]
_PROGRESS_MAX_CHARS = 800


class TaskController:
    def __init__(self) -> None:
        self._state = PlanState()
        self._sink: EventSink | None = None

    def start(self, plan: PlanState, sink: EventSink) -> None:
        self._state = deepcopy(plan)
        self._sink = sink

    def snapshot(self) -> PlanState:
        return deepcopy(self._state)

    async def _emit(self, event: RunEvent) -> None:
        if self._sink is None:
            return
        result: Any = self._sink(event)
        if inspect.isawaitable(result):
            await result

    async def set_plan(
        self,
        steps: list[PlanStepInput],
        goal: str = "",
        constraints: list[str] | None = None,
    ) -> str:
        if self._sink is None:
            raise RuntimeError("TaskController.set_plan called before start()")
        new_steps = [PlanStep.model_validate(step.model_dump()) for step in steps]
        previous_revision = self._state.revision
        self._state = PlanState(
            goal=goal.strip(),
            constraints=list(constraints or []),
            revision=previous_revision + 1,
            state_version=self._state.state_version + 1,
            lifecycle=PlanLifecycle.DRAFT,
            steps=new_steps,
        )
        event_type: type[RunEvent] = PlanCreated if previous_revision == 0 else PlanUpdated
        await self._emit(event_type(self.snapshot()))
        return "Plan updated."

    async def update_step(
        self,
        step_id: str,
        status: StepStatus,
        note: str | None = None,
        owner: str | None = None,
    ) -> str:
        if self._sink is None:
            raise RuntimeError("TaskController.update_step called before start()")
        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"unknown step id: {step_id}")
        current = self._state.steps[index]
        if current.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
            raise ValueError(f"finished step {step_id!r} cannot change status")
        if status in {StepStatus.IN_PROGRESS, StepStatus.COMPLETED}:
            statuses = {step.id: step.status for step in self._state.steps}
            incomplete = [
                dependency
                for dependency in current.depends_on
                if statuses[dependency] not in {StepStatus.COMPLETED, StepStatus.SKIPPED}
            ]
            if incomplete:
                raise ValueError(f"step {step_id!r} has incomplete dependencies: {incomplete}")
        if status is StepStatus.COMPLETED:
            receipts = {receipt.id: receipt for receipt in self._state.evidence}
            covered = {
                criterion_id
                for evidence_id in current.evidence_ids
                if (receipt := receipts.get(evidence_id)) is not None and receipt.passed
                for criterion_id in receipt.criterion_ids
            }
            missing = [
                criterion.id
                for criterion in current.acceptance_criteria
                if criterion.id not in covered
            ]
            if missing:
                raise ValueError(
                    "; ".join(
                        f"criterion {criterion_id} has no passing evidence"
                        for criterion_id in missing
                    )
                )
        updated = current.model_copy(update={"status": status, "note": note, "owner": owner})
        new_steps = list(self._state.steps)
        new_steps[index] = updated
        lifecycle = PlanLifecycle.EXECUTING
        if any(step.status is StepStatus.BLOCKED for step in new_steps):
            lifecycle = PlanLifecycle.BLOCKED
        elif new_steps and all(
            step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED} for step in new_steps
        ):
            lifecycle = PlanLifecycle.COMPLETED
        self._state = self._state.model_copy(
            update={
                "steps": new_steps,
                "state_version": self._state.state_version + 1,
                "lifecycle": lifecycle,
            }
        )
        await self._emit(PlanUpdated(self.snapshot()))
        return "Step updated."

    def record_evidence(self, receipt: EvidenceReceipt) -> None:
        if any(item.id == receipt.id for item in self._state.evidence):
            raise ValueError(f"duplicate evidence receipt: {receipt.id}")
        self._state = self._state.model_copy(
            update={
                "evidence": [*self._state.evidence, receipt],
                "state_version": self._state.state_version + 1,
            }
        )

    async def link_evidence(
        self,
        step_id: str,
        evidence_id: str,
        criterion_ids: list[str],
    ) -> str:
        if self._sink is None:
            raise RuntimeError("TaskController.link_evidence called before start()")
        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"unknown step id: {step_id}")
        receipt = next((item for item in self._state.evidence if item.id == evidence_id), None)
        if receipt is None:
            raise ValueError(f"unknown evidence receipt: {evidence_id}")
        valid = {item.id for item in self._state.steps[index].acceptance_criteria}
        requested = set(criterion_ids)
        if not requested or not requested <= valid:
            raise ValueError("criterion_ids must reference criteria on the selected step")
        receipts = list(self._state.evidence)
        receipt_index = next(index for index, item in enumerate(receipts) if item.id == evidence_id)
        receipts[receipt_index] = receipt.model_copy(
            update={"criterion_ids": list(dict.fromkeys([*receipt.criterion_ids, *criterion_ids]))}
        )
        step = self._state.steps[index]
        evidence_ids = list(dict.fromkeys([*step.evidence_ids, evidence_id]))
        steps = list(self._state.steps)
        steps[index] = step.model_copy(update={"evidence_ids": evidence_ids})
        self._state = self._state.model_copy(
            update={
                "steps": steps,
                "evidence": receipts,
                "state_version": self._state.state_version + 1,
            }
        )
        await self._emit(PlanUpdated(self.snapshot()))
        return "Evidence linked."

    async def attach_executor_evidence(
        self,
        step_id: str,
        evidence_id: str,
        criterion_ids: list[str] | None = None,
    ) -> None:
        """Attach trusted executor evidence, optionally covering criteria.

        This internal Interface is used by runtimes such as AgentOrchestrator;
        model-visible callers continue to use ``link_evidence``.
        """

        requested = list(criterion_ids or [])
        if requested:
            await self.link_evidence(step_id, evidence_id, requested)
            return
        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"unknown step id: {step_id}")
        if not any(item.id == evidence_id for item in self._state.evidence):
            raise ValueError(f"unknown evidence receipt: {evidence_id}")
        step = self._state.steps[index]
        steps = list(self._state.steps)
        steps[index] = step.model_copy(
            update={"evidence_ids": list(dict.fromkeys([*step.evidence_ids, evidence_id]))}
        )
        self._state = self._state.model_copy(
            update={"steps": steps, "state_version": self._state.state_version + 1}
        )
        await self._emit(PlanUpdated(self.snapshot()))

    async def report_progress(self, summary: str, next_action: str | None = None) -> str:
        if self._sink is None:
            raise RuntimeError("TaskController.report_progress called before start()")
        stripped = summary.strip()
        if not stripped:
            raise ValueError("progress summary must not be empty")
        if len(summary) > _PROGRESS_MAX_CHARS:
            raise ValueError(f"progress summary exceeds {_PROGRESS_MAX_CHARS} characters")
        await self._emit(ProgressReported(summary=summary, next_action=next_action))
        return "Progress reported."

    def _find_step_index(self, step_id: str) -> int | None:
        return next((index for index, step in enumerate(self._state.steps) if step.id == step_id), None)
