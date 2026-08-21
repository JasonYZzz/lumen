"""Thin TUI adapter over the same WorkspaceHost used by Web/headless."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from lumen.application import (
    ApproveAgentImport,
    ApprovePlan,
    CancelRun,
    CloseAgent,
    ContinueAgent,
    CreateSession,
    DecideApproval,
    DequeueRunInputs,
    ForkSessionAtTurn,
    InterruptAgent,
    ListAgents,
    ListCheckpoints,
    QueueRunInput,
    RejectAgentImport,
    RejectPlan,
    SendAgentMessage,
    SetApprovalMode,
    SetCollaborationMode,
    SetTranscriptDensity,
    StartRun,
    WaivePlanVerification,
    WorkspaceHost,
)
from lumen.application.events import event_from_payload
from lumen.context import CompactionCheckpointV1, CompactionCheckpointV2, ContextSummary
from lumen.events import (
    ApprovalRequest,
    ToolApprovalBatchPending,
    ToolApprovalPending,
)
from lumen.interactive_queue import QueuedMessage, QueueMode
from lumen.run_coordinator import CoordinatorState, RunInput
from lumen.runtime import ApprovalBatchHandler, ApprovalHandler, EventSink


@dataclass(frozen=True, slots=True)
class HostRunOutcome:
    status: str


class HostSessionAdapter:
    """Compatibility-shaped session view; all mutations cross Host commands."""

    def __init__(self, host: WorkspaceHost) -> None:
        self.host = host
        self.session_id: str | None = None
        self.active_run_id: str | None = None

    @property
    def state(self) -> CoordinatorState:
        if self.session_id is None:
            raise RuntimeError("host session adapter has no active session")
        loaded = self.host.resources.session_repository.load(self.session_id)
        summary = loaded.latest_compaction_summary
        checkpoint = loaded.latest_compaction_checkpoint
        return CoordinatorState(
            session=loaded.metadata,
            history=list(loaded.history),
            full_history=list(loaded.full_history),
            plan=loaded.plan,
            compaction_summary=summary if isinstance(summary, ContextSummary) else None,
            compaction_checkpoint=(
                checkpoint
                if isinstance(checkpoint, CompactionCheckpointV1 | CompactionCheckpointV2)
                else None
            ),
            compacted_prefix_length=loaded.compacted_prefix_length,
            compacted_source_end=loaded.compacted_source_end,
            last_user_input=loaded.turns[-1].user_input if loaded.turns else None,
            last_recovery_receipts=(
                tuple(loaded.turns[-1].recovery_receipts) if loaded.turns else ()
            ),
        )

    async def new_session(self) -> CoordinatorState:
        created = await self.host.dispatch(CreateSession())
        self.session_id = created.session_id
        return self.state

    async def resume(self, session_id: str) -> CoordinatorState:
        await self.host.snapshot(session_id)
        self.session_id = session_id
        return self.state

    async def set_modes(
        self,
        approval: Literal["manual", "accept_edits", "auto"],
        collaboration: Literal["default", "plan"],
    ) -> None:
        if self.session_id is None:
            raise RuntimeError("no active session")
        await self.host.dispatch(SetApprovalMode(self.session_id, approval))
        await self.host.dispatch(SetCollaborationMode(self.session_id, collaboration))

    async def set_collaboration(
        self, collaboration: Literal["default", "plan"]
    ) -> None:
        if self.session_id is None:
            raise RuntimeError("no active session")
        await self.host.dispatch(SetCollaborationMode(self.session_id, collaboration))

    async def set_transcript_density(
        self, density: Literal["normal", "verbose"]
    ) -> None:
        if self.session_id is None:
            raise RuntimeError("no active session")
        await self.host.dispatch(SetTranscriptDensity(self.session_id, density))

    async def enqueue_interactive(self, run_input: RunInput, mode: QueueMode) -> Any:
        if self.active_run_id is None:
            raise RuntimeError("no active run")
        return await self.host.dispatch(
            QueueRunInput(
                self.active_run_id,
                run_input.display_text,
                mode.value,
                model_prompt=run_input.model_prompt,
            )
        )

    async def dequeue_interactive(self) -> tuple[QueuedMessage, ...]:
        if self.active_run_id is None:
            return ()
        result = await self.host.dispatch(DequeueRunInputs(self.active_run_id))
        return tuple(
            QueuedMessage(
                id=str(item["id"]),
                text=str(item["text"]),
                model_prompt=str(item["model_prompt"]),
                mode=QueueMode(str(item["mode"])),
                byte_size=int(item["byte_size"]),
            )
            for item in result.data["items"]
        )

    async def list_child_runs(self) -> list[dict[str, Any]]:
        if self.session_id is None:
            return []
        result = await self.host.dispatch(ListAgents(self.session_id))
        return list(result.data.get("items", []))

    async def cancel_child_run(self, child_id: str) -> dict[str, Any]:
        result = await self.host.dispatch(InterruptAgent(child_id))
        child = result.data.get("agent", {})
        if isinstance(child, str):
            import json

            return dict(json.loads(child))
        return dict(child)

    async def send_agent_message(self, agent_id: str, message: str) -> dict[str, Any]:
        result = await self.host.dispatch(SendAgentMessage(agent_id, message))
        return dict(result.data)

    async def continue_agent(self, agent_id: str, task: str) -> dict[str, Any]:
        result = await self.host.dispatch(ContinueAgent(agent_id, task))
        return dict(result.data)

    async def approve_agent_import(self, agent_id: str) -> dict[str, Any]:
        result = await self.host.dispatch(ApproveAgentImport(agent_id))
        return dict(result.data)

    async def reject_agent_import(self, agent_id: str) -> dict[str, Any]:
        result = await self.host.dispatch(RejectAgentImport(agent_id))
        return dict(result.data)

    async def close_agent(self, agent_id: str, reason: str) -> dict[str, Any]:
        result = await self.host.dispatch(
            CloseAgent(agent_id, resolution="tui_resolution", reason=reason)
        )
        return dict(result.data)

    async def list_checkpoints(self) -> list[dict[str, Any]]:
        if self.session_id is None:
            return []
        result = await self.host.dispatch(ListCheckpoints(self.session_id))
        return list(result.data.get("items", []))

    async def work_state(self) -> dict[str, list[dict[str, Any]]]:
        if self.session_id is None:
            return {"work_products": [], "pending_effects": [], "recoverable_effects": []}
        snapshot = await self.host.snapshot(self.session_id)
        return {
            "work_products": snapshot.work_products,
            "pending_effects": snapshot.pending_effects,
            "recoverable_effects": snapshot.recoverable_effects,
        }

    async def waive_effect(self, effect_id: str, reason: str) -> dict[str, Any]:
        if self.session_id is None:
            raise RuntimeError("no active session")
        result = await self.host.dispatch(
            WaivePlanVerification(self.session_id, (effect_id,), reason)
        )
        return dict(result.data)

    async def fork_at_checkpoint(self, turn_index: int) -> str:
        if self.session_id is None:
            raise RuntimeError("no active session")
        result = await self.host.dispatch(ForkSessionAtTurn(self.session_id, turn_index))
        return result.session_id

    async def run(
        self,
        run_input: RunInput,
        emit: EventSink,
        approve: ApprovalHandler,
        approve_batch: ApprovalBatchHandler | None = None,
    ) -> HostRunOutcome | None:
        if self.session_id is None:
            raise RuntimeError("no active session")
        started = await self.host.dispatch(
            StartRun(
                self.session_id,
                run_input.display_text,
                f"tui-{uuid4()}",
                model_prompt=run_input.model_prompt,
            )
        )
        return await self._consume(started.run_id, emit, approve, approve_batch)

    async def approve_plan(
        self,
        revision: int,
        approval_mode: Literal["manual", "accept_edits", "auto"],
        emit: EventSink,
        approve: ApprovalHandler,
        approve_batch: ApprovalBatchHandler | None = None,
    ) -> HostRunOutcome | None:
        if self.session_id is None:
            raise RuntimeError("no active session")
        await self.host.dispatch(SetApprovalMode(self.session_id, approval_mode))
        started = await self.host.dispatch(
            ApprovePlan(self.session_id, revision, f"tui-plan-approve-{uuid4()}")
        )
        return await self._consume(started.run_id, emit, approve, approve_batch)

    async def reject_plan(
        self,
        revision: int,
        feedback: str,
        emit: EventSink,
        approve: ApprovalHandler,
        approve_batch: ApprovalBatchHandler | None = None,
    ) -> HostRunOutcome | None:
        if self.session_id is None:
            raise RuntimeError("no active session")
        started = await self.host.dispatch(
            RejectPlan(
                self.session_id,
                revision,
                feedback,
                f"tui-plan-reject-{uuid4()}",
            )
        )
        return await self._consume(started.run_id, emit, approve, approve_batch)

    async def _consume(
        self,
        run_id: str,
        emit: EventSink,
        approve: ApprovalHandler,
        approve_batch: ApprovalBatchHandler | None,
    ) -> HostRunOutcome | None:
        self.active_run_id = run_id
        status = "running"
        try:
            async for envelope in self.host.subscribe(run_id):
                event = event_from_payload(envelope.type, envelope.data)
                if isinstance(event, ToolApprovalPending):
                    decision = await approve(
                        ApprovalRequest(
                            event.call_id,
                            event.name,
                            event.args,
                            event.origin,
                            event.risk,
                        )
                    )
                    await self.host.dispatch(
                        DecideApproval(
                            run_id,
                            event.call_id,
                            decision.approved,
                            decision.remember_scope,
                        )
                    )
                    continue
                if isinstance(event, ToolApprovalBatchPending):
                    if approve_batch is None:
                        decisions = {
                            request.call_id: await approve(request)
                            for request in event.requests
                        }
                    else:
                        decisions = await approve_batch(event.requests)
                    for call_id, decision in decisions.items():
                        await self.host.dispatch(
                            DecideApproval(
                                run_id,
                                call_id,
                                decision.approved,
                                decision.remember_scope,
                            )
                        )
                    continue
                await emit(event)
                if envelope.type == "run.failed":
                    status = "failed"
                elif envelope.type == "run.cancelled":
                    status = "cancelled"
                elif envelope.type == "run.waiting_for_user":
                    status = "waiting_for_user"
                elif envelope.type == "run.completed":
                    status = "completed"
        except asyncio.CancelledError:
            await asyncio.shield(self.host.dispatch(CancelRun(run_id)))
            raise
        finally:
            self.active_run_id = None
        return None if status == "failed" else HostRunOutcome(status)
