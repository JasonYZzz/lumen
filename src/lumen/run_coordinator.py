"""Session-bound agent runs behind one small, persistence-owning interface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage

from lumen.context import ContextSummary
from lumen.events import InputDequeued, InputQueued, RunEvent, RunStarted, TimelineEventRecord
from lumen.interactive_queue import QueuedMessage, QueueMode
from lumen.plan import PlanState
from lumen.runtime import (
    AgentRuntime,
    ApprovalHandler,
    EventSink,
    RunOutcome,
    get_partial_outcome,
)
from lumen.sessions import SessionMetadata, SessionRepository


@dataclass(frozen=True, slots=True)
class CoordinatorState:
    session: SessionMetadata
    history: list[ModelMessage] = field(default_factory=list[ModelMessage])
    plan: PlanState = field(default_factory=PlanState)
    compaction_summary: ContextSummary | None = None
    last_user_input: str | None = None


@dataclass(frozen=True, slots=True)
class RunInput:
    display_text: str
    model_prompt: str


class RunCoordinator:
    """Own active session state and persist every terminal run outcome."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        agent_name: str,
        model_id: Callable[[], str],
        runtime: Callable[[], AgentRuntime | None],
    ) -> None:
        self._repository = repository
        self._agent_name = agent_name
        self._model_id = model_id
        self._runtime = runtime
        self._state: CoordinatorState | None = None
        self._active_emit: EventSink | None = None

    @property
    def state(self) -> CoordinatorState:
        if self._state is None:
            raise RuntimeError("run coordinator has no active session")
        return self._state

    def new_session(self) -> CoordinatorState:
        session = self._repository.create(agent_name=self._agent_name, model_id=self._model_id())
        self._state = CoordinatorState(session=session)
        return self._state

    def resume(self, session_id: str) -> CoordinatorState:
        loaded = self._repository.load(session_id)
        summary = loaded.latest_compaction_summary
        self._state = CoordinatorState(
            session=loaded.metadata,
            history=list(loaded.history),
            plan=loaded.plan,
            compaction_summary=summary if isinstance(summary, ContextSummary) else None,
            last_user_input=loaded.turns[-1].user_input if loaded.turns else None,
        )
        return self._state

    async def run(
        self,
        run_input: RunInput | str,
        emit: EventSink,
        approve: ApprovalHandler,
    ) -> RunOutcome | None:
        if isinstance(run_input, str):
            run_input = RunInput(run_input, run_input)
        state = self.state
        runtime = self._runtime()
        if runtime is None:
            raise RuntimeError("runtime is not available")
        timeline_events: list[TimelineEventRecord] = []

        async def record_and_emit(event: RunEvent) -> None:
            visible_event: RunEvent = event
            if isinstance(event, RunStarted):
                visible_event = RunStarted(run_input.display_text)
            timeline_events.append(
                TimelineEventRecord.from_event(visible_event, sequence=len(timeline_events) + 1)
            )
            await emit(visible_event)

        self._active_emit = record_and_emit
        try:
            outcome = await runtime.run(
                run_input.model_prompt,
                state.history,
                record_and_emit,
                approve,
                plan=state.plan,
                previous_summary=state.compaction_summary,
                session_id=state.session.id,
            )
        except asyncio.CancelledError as error:
            self._append_partial(run_input, error, status="cancelled", timeline_events=timeline_events)
            raise
        except Exception as error:
            self._append_partial(run_input, error, status="failed", timeline_events=timeline_events)
            return None
        finally:
            self._active_emit = None

        summary = state.compaction_summary
        if outcome.compaction is not None:
            summary = outcome.compaction.summary
        self._state = CoordinatorState(
            session=state.session,
            history=list(outcome.active_history),
            plan=outcome.plan,
            compaction_summary=summary,
            last_user_input=run_input.display_text,
        )
        self._repository.append_turn(
            state.session.id,
            user_input=run_input.display_text,
            messages=outcome.new_messages,
            approvals=outcome.approvals,
            usage=outcome.usage,
            status="completed",
            plan=outcome.plan,
            diagnostics=outcome.diagnostics,
            compaction=outcome.compaction,
            timeline_events=timeline_events,
        )
        return outcome

    async def enqueue_interactive(self, run_input: RunInput, mode: QueueMode) -> QueuedMessage:
        runtime = self._runtime()
        if runtime is None or self._active_emit is None:
            raise RuntimeError("no active run accepts interactive input")
        message = runtime.interactive_queue.enqueue(
            run_input.display_text,
            run_input.model_prompt,
            mode,
        )
        await self._active_emit(InputQueued(message.id, message.text, message.mode.value))
        return message

    async def dequeue_interactive(self) -> tuple[QueuedMessage, ...]:
        runtime = self._runtime()
        if runtime is None:
            return ()
        messages = runtime.interactive_queue.dequeue_all()
        if self._active_emit is not None:
            for message in messages:
                await self._active_emit(InputDequeued(message.id, message.text, message.mode.value))
        return messages

    def _append_partial(
        self,
        run_input: RunInput,
        error: BaseException,
        *,
        status: str,
        timeline_events: list[TimelineEventRecord],
    ) -> None:
        state = self.state
        partial = get_partial_outcome(error)
        latest_plan = partial.plan if partial is not None else state.plan
        self._state = CoordinatorState(
            session=state.session,
            history=list(state.history),
            plan=latest_plan,
            compaction_summary=state.compaction_summary,
            last_user_input=run_input.display_text,
        )
        self._repository.append_turn(
            state.session.id,
            user_input=run_input.display_text,
            messages=[],
            approvals=partial.approvals if partial is not None else [],
            usage=partial.usage if partial is not None else {},
            status=status,
            plan=latest_plan,
            diagnostics=partial.diagnostics if partial is not None else [],
            error_message=partial.message if partial is not None else str(error),
            partial_text=partial.partial_text if partial is not None else None,
            retryable=partial.retryable if partial is not None else False,
            timeline_events=timeline_events,
        )


__all__ = ["CoordinatorState", "RunCoordinator", "RunInput"]
