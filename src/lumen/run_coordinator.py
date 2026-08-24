"""Session-bound agent runs behind one small, persistence-owning interface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage

from lumen.attachments import AttachmentRef
from lumen.context import CompactionCheckpointV1, CompactionCheckpointV2, ContextSummary
from lumen.events import (
    InputDequeued,
    InputQueued,
    RunEvent,
    RunFailed,
    RunStarted,
    TimelineEventRecord,
)
from lumen.interactive_queue import QueuedMessage, QueueMode
from lumen.plan import PlanState
from lumen.runtime import (
    AgentRuntime,
    ApprovalBatchHandler,
    ApprovalHandler,
    CompletionPolicy,
    EventSink,
    RunOutcome,
    get_partial_outcome,
)
from lumen.sessions import SessionMetadata, SessionRepository, recoverable_orphaned_input


@dataclass(frozen=True, slots=True)
class CoordinatorState:
    session: SessionMetadata
    history: list[ModelMessage] = field(default_factory=list[ModelMessage])
    full_history: list[ModelMessage] = field(default_factory=list[ModelMessage])
    plan: PlanState = field(default_factory=PlanState)
    compaction_summary: ContextSummary | None = None
    compaction_checkpoint: CompactionCheckpointV1 | CompactionCheckpointV2 | None = None
    compacted_prefix_length: int = 0
    compacted_source_end: int = 0
    last_user_input: str | None = None
    last_recovery_receipts: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class RunInput:
    display_text: str
    model_prompt: str
    is_retry: bool = False
    interaction_id: str | None = None
    attachments: tuple[AttachmentRef, ...] = ()


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
            full_history=list(loaded.full_history),
            plan=loaded.plan,
            compaction_summary=summary if isinstance(summary, ContextSummary) else None,
            compaction_checkpoint=(
                loaded.latest_compaction_checkpoint
                if isinstance(
                    loaded.latest_compaction_checkpoint,
                    (CompactionCheckpointV1, CompactionCheckpointV2),
                )
                else None
            ),
            compacted_prefix_length=loaded.compacted_prefix_length,
            compacted_source_end=loaded.compacted_source_end,
            last_user_input=(
                loaded.turns[-1].user_input
                if loaded.turns
                else recoverable_orphaned_input(loaded)
            ),
            last_recovery_receipts=(tuple(loaded.turns[-1].recovery_receipts) if loaded.turns else ()),
        )
        return self._state

    def replace_plan(self, plan: PlanState) -> None:
        """Replace only the in-memory plan before the next durably recorded run."""

        state = self.state
        self._state = CoordinatorState(
            session=state.session,
            history=state.history,
            full_history=state.full_history,
            plan=plan,
            compaction_summary=state.compaction_summary,
            compaction_checkpoint=state.compaction_checkpoint,
            compacted_prefix_length=state.compacted_prefix_length,
            compacted_source_end=state.compacted_source_end,
            last_user_input=state.last_user_input,
            last_recovery_receipts=state.last_recovery_receipts,
        )

    def persist_run_start(
        self,
        user_input: str,
        interaction_id: str,
        attachments: tuple[AttachmentRef, ...] = (),
    ) -> None:
        """Durably accept one input before model execution or tool effects begin."""

        state = self.state
        self._repository.append_turn_started(
            state.session.id,
            user_input=user_input,
            interaction_id=interaction_id,
            plan=state.plan,
            attachments=attachments,
        )

    def persist_unhandled_failure(
        self,
        user_input: str,
        interaction_id: str,
        error_message: str,
    ) -> bool:
        """Close a running projection when terminal persistence itself failed.

        The normal runtime path owns rich messages, usage and diagnostics. This
        fallback is intentionally minimal: it runs only while the durable read
        projection still contains the matching ``running`` turn.
        """

        state = self.state
        loaded = self._repository.load(state.session.id)
        running = next(
            (
                turn
                for turn in loaded.turns
                if turn.interaction_id == interaction_id and turn.status == "running"
            ),
            None,
        )
        if running is None:
            return False
        timeline_events = list(running.timeline_events)
        timeline_events.append(
            TimelineEventRecord.from_event(
                RunFailed(error_message),
                sequence=len(timeline_events) + 1,
            )
        )
        self._repository.append_turn(
            state.session.id,
            user_input=user_input,
            messages=[],
            approvals=[],
            usage={},
            status="failed",
            plan=state.plan,
            error_message=error_message,
            timeline_events=timeline_events,
            interaction_id=interaction_id,
            attachments=tuple(running.attachments),
        )
        self._state = CoordinatorState(
            session=state.session,
            history=state.history,
            full_history=state.full_history,
            plan=state.plan,
            compaction_summary=state.compaction_summary,
            compaction_checkpoint=state.compaction_checkpoint,
            compacted_prefix_length=state.compacted_prefix_length,
            compacted_source_end=state.compacted_source_end,
            last_user_input=user_input,
            last_recovery_receipts=state.last_recovery_receipts,
        )
        return True
        self._state = CoordinatorState(
            session=state.session,
            history=state.history,
            full_history=state.full_history,
            plan=state.plan,
            compaction_summary=state.compaction_summary,
            compaction_checkpoint=state.compaction_checkpoint,
            compacted_prefix_length=state.compacted_prefix_length,
            compacted_source_end=state.compacted_source_end,
            last_user_input=user_input,
            last_recovery_receipts=state.last_recovery_receipts,
        )

    async def run(
        self,
        run_input: RunInput | str,
        emit: EventSink,
        approve: ApprovalHandler,
        approve_batch: ApprovalBatchHandler | None = None,
        *,
        completion_policy: CompletionPolicy | None = None,
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
                approve_batch,
                plan=state.plan,
                previous_summary=state.compaction_summary,
                previous_checkpoint=state.compaction_checkpoint,
                compacted_prefix_length=state.compacted_prefix_length,
                source_offset=state.compacted_source_end,
                source_history=state.full_history,
                episode_documents=self._repository.retrieve_checkpoint_episodes(
                    state.session.id,
                    run_input.model_prompt,
                ),
                session_id=state.session.id,
                recovery_receipts=state.last_recovery_receipts if run_input.is_retry else (),
                completion_policy=completion_policy,
                attachments=run_input.attachments,
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
        checkpoint = state.compaction_checkpoint
        compacted_prefix_length = state.compacted_prefix_length
        compacted_source_end = state.compacted_source_end
        if outcome.compaction is not None:
            summary = outcome.compaction.summary
            candidate = getattr(outcome.compaction, "checkpoint", None)
            if isinstance(candidate, (CompactionCheckpointV1, CompactionCheckpointV2)):
                checkpoint = candidate
                compacted_source_end = candidate.source_end
            else:
                compacted_source_end += outcome.compaction.source_message_count
            compacted_prefix_length = len(outcome.compaction.active_history)
        next_state = CoordinatorState(
            session=state.session,
            history=list(outcome.active_history),
            full_history=[*state.full_history, *outcome.new_messages],
            plan=outcome.plan,
            compaction_summary=summary,
            compaction_checkpoint=checkpoint,
            compacted_prefix_length=compacted_prefix_length,
            compacted_source_end=compacted_source_end,
            last_user_input=run_input.display_text,
            last_recovery_receipts=tuple(outcome.recovery_receipts),
        )
        # Publish the in-memory transition only after the append-only record is
        # durable. A failed append therefore cannot move the session cursor or
        # checkpoint ahead of JSONL.
        try:
            self._repository.append_turn(
                state.session.id,
                user_input=run_input.display_text,
                messages=outcome.new_messages,
                approvals=outcome.approvals,
                usage=outcome.usage,
                status=outcome.status,
                plan=outcome.plan,
                diagnostics=outcome.diagnostics,
                compaction=outcome.compaction,
                timeline_events=timeline_events,
                recovery_receipts=outcome.recovery_receipts,
                request_receipts=outcome.request_receipts,
                interaction_id=run_input.interaction_id,
                attachments=run_input.attachments,
            )
        except BaseException as error:
            if runtime.context_engine is not None and outcome.context_fingerprint is not None:
                runtime.context_engine.discard_unpersisted(
                    state.session.id,
                    outcome.context_fingerprint,
                )
            if isinstance(error, Exception):
                self._append_terminal_persistence_failure(
                    run_input,
                    error,
                    plan=outcome.plan,
                    timeline_events=timeline_events,
                )
            raise
        if runtime.context_engine is not None and outcome.context_fingerprint is not None:
            runtime.context_engine.confirm_persisted(
                state.session.id,
                outcome.context_fingerprint,
                outcome.new_messages,
            )
        self._state = next_state
        if runtime.context_engine is not None and runtime.context_engine.memory is not None:
            runtime.context_engine.memory.schedule_session(state.session.id)
        return outcome

    def _append_terminal_persistence_failure(
        self,
        run_input: RunInput,
        error: Exception,
        *,
        plan: PlanState,
        timeline_events: list[TimelineEventRecord],
    ) -> None:
        """Preserve the visible transcript when rich provider messages cannot serialize."""

        message = f"terminal_persistence_failed: {type(error).__name__}: {error}"
        fallback_events = list(timeline_events)
        fallback_events.append(
            TimelineEventRecord.from_event(
                RunFailed(message),
                sequence=len(fallback_events) + 1,
            )
        )
        try:
            self._repository.append_turn(
                self.state.session.id,
                user_input=run_input.display_text,
                messages=[],
                approvals=[],
                usage={},
                status="failed",
                plan=plan,
                error_message=message,
                timeline_events=fallback_events,
                interaction_id=run_input.interaction_id,
                attachments=run_input.attachments,
            )
        except Exception:
            # The Host has one final minimal running-turn reconciliation path.
            # Preserve the original persistence exception for its audit text.
            return

    async def enqueue_interactive(self, run_input: RunInput, mode: QueueMode) -> QueuedMessage:
        runtime = self._runtime()
        if runtime is None or self._active_emit is None:
            raise RuntimeError("no active run accepts interactive input")
        message = runtime.interactive_queue.enqueue(
            run_input.display_text,
            run_input.model_prompt,
            mode,
            run_input.attachments,
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
        next_state = CoordinatorState(
            session=state.session,
            history=list(state.history),
            full_history=list(state.full_history),
            plan=latest_plan,
            compaction_summary=state.compaction_summary,
            compaction_checkpoint=state.compaction_checkpoint,
            compacted_prefix_length=state.compacted_prefix_length,
            compacted_source_end=state.compacted_source_end,
            last_user_input=run_input.display_text,
            last_recovery_receipts=(
                tuple(partial.recovery_receipts) if partial is not None else state.last_recovery_receipts
            ),
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
            recovery_receipts=partial.recovery_receipts if partial is not None else (),
            request_receipts=partial.request_receipts if partial is not None else (),
            interaction_id=run_input.interaction_id,
            attachments=run_input.attachments,
        )
        self._state = next_state


__all__ = ["CoordinatorState", "RunCoordinator", "RunInput"]
