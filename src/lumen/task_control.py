"""Per-run task controller exposing side-effect-free control tools to the model.

The controller owns the plan state for a single ``AgentRuntime.run`` invocation.
It is bound to one event sink so the tools it exposes (``set_plan``,
``update_step``, ``report_progress``) cannot escape the run that created them,
and it emits typed events the TUI renders without inspecting model-internal
reasoning.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from lumen.events import (
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunEvent,
)
from lumen.plan import PlanState, PlanStep, PlanStepInput, StepStatus

EventSink = Callable[[RunEvent], None] | Callable[[RunEvent], Awaitable[None]]

# Public progress summaries are bounded so the model cannot dump reasoning or
# endless prose into the user-visible progress feed.
_PROGRESS_MAX_CHARS = 800


class TaskController:
    """Bound controller for one run; tools are async methods on this instance."""

    def __init__(self) -> None:
        self._state = PlanState()
        self._sink: EventSink | None = None

    def start(self, plan: PlanState, sink: EventSink) -> None:
        """Bind this controller to a run, replacing any prior state.

        ``plan`` is taken by value so callers (e.g. resume paths) can seed the
        controller with restored state without leaking mutable references.
        """

        self._state = deepcopy(plan)
        self._sink = sink

    def snapshot(self) -> PlanState:
        """Return an immutable copy of the current plan state."""

        return deepcopy(self._state)

    async def _emit(self, event: RunEvent) -> None:
        if self._sink is None:
            return
        result: Any = self._sink(event)
        if inspect.isawaitable(result):
            await result

    async def set_plan(self, steps: list[PlanStepInput]) -> str:
        """Replace the plan atomically and emit one created/updated event.

        Rejects duplicate IDs before any state mutation.
        """

        if self._sink is None:
            raise RuntimeError("TaskController.set_plan called before start()")

        seen: set[str] = set()
        for step in steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id}")
            seen.add(step.id)

        new_steps = [PlanStep(id=step.id, title=step.title) for step in steps]
        previous_revision = self._state.revision
        self._state = PlanState(revision=previous_revision + 1, steps=new_steps)
        event_type: type[RunEvent] = PlanCreated if previous_revision == 0 else PlanUpdated
        await self._emit(event_type(self.snapshot()))
        return "Plan updated."

    async def update_step(
        self,
        step_id: str,
        status: StepStatus,
        note: str | None = None,
    ) -> str:
        """Transition one step to ``status``, validating the move.

        Enforces the public state machine:

        - exactly one ``in_progress`` step at a time
        - no transition out of ``completed`` (re-running finished work must be a
          new plan)
        - the step id must already exist
        """

        if self._sink is None:
            raise RuntimeError("TaskController.update_step called before start()")

        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"unknown step id: {step_id}")

        current = self._state.steps[index]
        if current.status is StepStatus.COMPLETED:
            raise ValueError(f"completed step {step_id!r} cannot change status")

        if status is StepStatus.IN_PROGRESS and self._active_step_id() not in (None, step_id):
            raise ValueError(f"another step is already in_progress: {self._active_step_id()}")

        updated_step = current.model_copy(update={"status": status, "note": note})
        new_steps = list(self._state.steps)
        new_steps[index] = updated_step
        # Bump revision so consumers can detect step transitions, not just
        # full plan replacements. Previously this copied revision verbatim,
        # making step updates invisible to revision-tracking consumers.
        self._state = PlanState(revision=self._state.revision + 1, steps=new_steps)
        await self._emit(PlanUpdated(self.snapshot()))
        return "Step updated."

    async def report_progress(self, summary: str, next_action: str | None = None) -> str:
        """Emit a single public, bounded progress note.

        Empty or whitespace-only summaries and text over the size limit are
        rejected before any event is sent.
        """

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
        for index, step in enumerate(self._state.steps):
            if step.id == step_id:
                return index
        return None

    def _active_step_id(self) -> str | None:
        for step in self._state.steps:
            if step.status is StepStatus.IN_PROGRESS:
                return step.id
        return None
