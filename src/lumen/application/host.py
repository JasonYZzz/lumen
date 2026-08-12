from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, overload
from uuid import uuid4

from lumen.approval import ApprovalDecision, ApprovalMode, ApprovalPolicy
from lumen.collaboration import (
    CollaborationMode,
    PlanReviewStatus,
    SessionSettingsState,
    apply_collaboration_context,
)
from lumen.context import ContextCompactCommand, ContextMemoryCommand, ContextReportCommand
from lumen.events import (
    AgentLifecycleChanged,
    ApprovalRequest,
    PlanReviewPending,
    PlanReviewResolved,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunWaitingForUser,
    ToolApprovalBatchPending,
    ToolApprovalPending,
)
from lumen.files import expand_file_mentions
from lumen.interactive_queue import QueueMode
from lumen.live import LiveConnectRequest, LiveEvent
from lumen.live.manager import LiveSessionManager
from lumen.plan import EvidenceKind, EvidenceReceipt, PlanLifecycle
from lumen.run_coordinator import RunCoordinator, RunInput
from lumen.runtime import AgentRuntime, CompletionPolicy, ToolApproval
from lumen.sessions import SessionMetadata, SessionRepository
from lumen.skills import expand_skill_for_message
from lumen.timeline import RepositoryTimelineAdapter, TimelineStore
from lumen.tools.gateway import CapabilityApproval
from lumen.tools.workspace import Workspace

from .events import EventJournal
from .models import (
    ApprovalAlreadyResolvedError,
    ApprovalStateError,
    ApproveAgentImport,
    ApproveChildImport,
    ApprovePlan,
    CancelChildRun,
    CancelClarification,
    CancelRun,
    CloseAgent,
    CloseChildRun,
    CommandAcknowledged,
    CommandResult,
    ContextControl,
    ContinueAgent,
    CreateSession,
    DecideApproval,
    DequeueRunInputs,
    EndLiveSession,
    EventEnvelope,
    ForkSessionAtTurn,
    GetBootstrap,
    InterruptAgent,
    InterruptLiveSession,
    InvalidStateError,
    InvokePrompt,
    InvokeSkill,
    ListAgents,
    ListCheckpoints,
    ListChildRuns,
    ListContextSources,
    ListHooks,
    ListMcpPrompts,
    ListSessions,
    LiveStartedResult,
    QueueRunInput,
    RejectAgentImport,
    RejectChildImport,
    RejectPlan,
    RetryRun,
    RunNotFoundError,
    RunStartedResult,
    SelectModel,
    SendAgentMessage,
    SessionCreated,
    SessionList,
    SessionNotFoundError,
    SessionSnapshot,
    SessionSummary,
    SetApprovalMode,
    SetCollaborationMode,
    SetContextSource,
    SetTranscriptDensity,
    StartLiveSession,
    StartRun,
    WaivePlanVerification,
    WorkspaceBootstrap,
    WorkspaceBusyError,
    WorkspaceCommand,
)


class WorkspaceResources(Protocol):
    workspace: Path
    session_repository: SessionRepository
    runtime: AgentRuntime | None
    config: Any
    tool_metadata: dict[str, dict[str, str]]
    warnings: list[str]
    skills: list[Any]
    agent_orchestrator: Any

    async def open(self) -> Any: ...
    async def close(self) -> None: ...
    def active_model_config(self) -> Any: ...
    def active_model_name(self) -> str: ...
    def available_models(self) -> list[str]: ...
    def mcp_summary(self) -> list[dict[str, object]]: ...
    def context_source_summary(self, session_id: str) -> list[dict[str, str]]: ...
    def mcp_prompt_summary(self) -> list[dict[str, object]]: ...
    async def render_mcp_prompt(self, reference: str, arguments: dict[str, str]) -> str: ...
    def hook_summary(self) -> list[dict[str, object]]: ...
    def summary(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class _PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ToolApproval]
    decision: bool | None = None


@dataclass(slots=True)
class _RunRecord:
    id: str
    session_id: str
    client_request_id: str
    journal: EventJournal
    task: asyncio.Task[None] | None = None
    approvals: dict[str, _PendingApproval] = field(default_factory=dict[str, _PendingApproval])
    status: str = "running"


@dataclass(slots=True)
class _SessionActor:
    coordinator: RunCoordinator
    metadata: SessionMetadata
    settings: SessionSettingsState
    active_run_id: str | None = None
    approval_keys: set[str] = field(default_factory=set[str])


class WorkspaceHost:
    """Own workspace-scoped interactive agent state behind one small interface."""

    def __init__(self, resources: WorkspaceResources) -> None:
        self.resources = resources
        self._sessions: dict[str, _SessionActor] = {}
        self._runs: dict[str, _RunRecord] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._active_run_id: str | None = None
        self._state_lock = asyncio.Lock()
        self._opened = False
        self._approval_policy = ApprovalPolicy()
        self._live_execution_id: str | None = None
        self._live_approvals: dict[str, dict[str, _PendingApproval]] = {}
        self._live_manager = getattr(resources, "live_manager", None)
        if self._live_manager is not None:
            self._live_manager.bind_approval_handler(self._request_live_approval)
            self._live_manager.bind_execution_lease(
                self._acquire_live_execution,
                self._release_live_execution,
            )

    async def open(self) -> WorkspaceHost:
        if not self._opened:
            await self.resources.open()
            self._opened = True
        return self

    async def close(self) -> None:
        if self._active_run_id is not None:
            try:
                await asyncio.wait_for(self._cancel(self._active_run_id), timeout=10)
            except TimeoutError:
                pass
            except RunNotFoundError:
                pass
        for live_session_id in tuple(self._live_approvals):
            self._resolve_live_pending(
                live_session_id,
                approved=False,
                message="Live session closed",
            )
        if self._opened:
            await self.resources.close()
            self._opened = False

    @overload
    async def dispatch(self, command: CreateSession) -> SessionCreated: ...

    @overload
    async def dispatch(self, command: ForkSessionAtTurn) -> SessionCreated: ...

    @overload
    async def dispatch(self, command: GetBootstrap) -> WorkspaceBootstrap: ...

    @overload
    async def dispatch(self, command: ListSessions) -> SessionList: ...

    @overload
    async def dispatch(
        self, command: StartRun | RetryRun | InvokeSkill | InvokePrompt | ApprovePlan | RejectPlan
    ) -> RunStartedResult: ...

    @overload
    async def dispatch(self, command: StartLiveSession) -> LiveStartedResult: ...

    @overload
    async def dispatch(
        self,
        command: (
            CancelRun
            | InterruptLiveSession
            | EndLiveSession
            | DecideApproval
            | QueueRunInput
            | DequeueRunInputs
            | SetApprovalMode
            | SetCollaborationMode
            | SetTranscriptDensity
            | SelectModel
            | ContextControl
            | SetContextSource
            | CancelClarification
            | ListContextSources
            | ListMcpPrompts
            | ListHooks
            | ListChildRuns
            | ListAgents
            | SendAgentMessage
            | ContinueAgent
            | InterruptAgent
            | CloseAgent
            | ApproveAgentImport
            | RejectAgentImport
            | ListCheckpoints
            | CancelChildRun
            | ApproveChildImport
            | RejectChildImport
            | CloseChildRun
            | WaivePlanVerification
        ),
    ) -> CommandAcknowledged: ...

    async def dispatch(self, command: WorkspaceCommand) -> CommandResult:
        if isinstance(command, GetBootstrap):
            return self._bootstrap()
        if isinstance(command, ListSessions):
            return self._list_sessions()
        if isinstance(command, CreateSession):
            return self._create_session()
        if isinstance(command, StartRun):
            return await self._start_run(command)
        if isinstance(command, StartLiveSession):
            return await self._start_live_session(command)
        if isinstance(command, InterruptLiveSession):
            manager = self._require_live_manager()
            self.live_snapshot(command.live_session_id)
            result = await manager.interrupt(command.live_session_id)
            return CommandAcknowledged("interrupted", result.state.model_dump(mode="json"))
        if isinstance(command, EndLiveSession):
            manager = self._require_live_manager()
            self.live_snapshot(command.live_session_id)
            self._resolve_live_pending(
                command.live_session_id,
                approved=False,
                message="Live session ended",
            )
            result = await manager.end(command.live_session_id)
            return CommandAcknowledged("ended", result.state.model_dump(mode="json"))
        if isinstance(command, ListCheckpoints):
            session = self.resources.session_repository.load(command.session_id)
            items: list[dict[str, Any]] = []
            for index, turn in enumerate(session.turns):
                mutations = sum(
                    str(receipt.get("risk")) in {"write", "execute"} for receipt in turn.recovery_receipts
                )
                items.append(
                    {
                        "index": index,
                        "created_at": turn.created_at,
                        "status": turn.status,
                        "prompt": turn.user_input,
                        "receipt_count": len(turn.recovery_receipts),
                        "mutation_count": mutations,
                    }
                )
            return CommandAcknowledged("ok", {"items": items})
        if isinstance(command, ForkSessionAtTurn):
            if self._active_run_id is not None:
                raise WorkspaceBusyError(
                    "cannot rewind while an agent run is active",
                    details={"run_id": self._active_run_id},
                )
            created = self.resources.session_repository.fork(
                command.session_id, through_turn=command.through_turn
            )
            return SessionCreated(created.id)
        if isinstance(command, CancelRun):
            return await self._cancel(command.run_id)
        if isinstance(command, DecideApproval):
            return self._decide_approval(command)
        if isinstance(command, QueueRunInput):
            return await self._queue_input(command)
        if isinstance(command, DequeueRunInputs):
            record = self._run(command.run_id)
            actor = self._actor(record.session_id)
            messages = await actor.coordinator.dequeue_interactive()
            return CommandAcknowledged(
                "dequeued",
                {"items": [asdict(message) for message in messages]},
            )
        if isinstance(command, SetApprovalMode):
            return self._set_approval_mode(command)
        if isinstance(command, SetCollaborationMode):
            return self._set_collaboration_mode(command)
        if isinstance(command, SetTranscriptDensity):
            actor = self._actor(command.session_id)
            actor.settings = actor.settings.model_copy(update={"transcript_density": command.density})
            self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
            return CommandAcknowledged("updated", {"transcript_density": command.density})
        if isinstance(command, ApprovePlan):
            return await self._approve_plan(command)
        if isinstance(command, RejectPlan):
            return await self._reject_plan(command)
        if isinstance(command, WaivePlanVerification):
            return self._waive_plan_verification(command)
        if isinstance(command, ListChildRuns):
            manager = self._child_manager()
            return CommandAcknowledged(
                "ok",
                {"items": [item.model_dump(mode="json") for item in manager.list(command.session_id)]},
            )
        if isinstance(command, ListAgents):
            return CommandAcknowledged(
                "ok",
                {
                    "items": [
                        self._agent_view(item)
                        for item in self.resources.agent_orchestrator.list(command.session_id)
                    ]
                },
            )
        if isinstance(command, SendAgentMessage):
            value = await self.resources.agent_orchestrator.send_message(command.agent_id, command.message)
            return CommandAcknowledged("queued", {"agent": value})
        if isinstance(command, ContinueAgent):
            value = await self.resources.agent_orchestrator.followup_task(command.agent_id, command.task)
            return CommandAcknowledged("queued", {"agent": value})
        if isinstance(command, InterruptAgent):
            value = await self.resources.agent_orchestrator.interrupt_agent(command.agent_id)
            return CommandAcknowledged("interrupted", {"agent": value})
        if isinstance(command, CloseAgent):
            value = await self.resources.agent_orchestrator.close_agent(
                command.agent_id,
                resolution=command.resolution,
                reason=command.reason,
            )
            return CommandAcknowledged("closed", {"agent": value})
        if isinstance(command, ApproveAgentImport):
            value = await self.resources.agent_orchestrator.approve_import(command.agent_id)
            return CommandAcknowledged(
                value.status.value,
                {"agent": value.model_dump(mode="json")},
            )
        if isinstance(command, RejectAgentImport):
            value = await self.resources.agent_orchestrator.reject_import(command.agent_id)
            return CommandAcknowledged("rejected", {"agent": value.model_dump(mode="json")})
        if isinstance(command, CancelChildRun):
            value = await self._child_manager().cancel_child(command.child_id)
            return CommandAcknowledged("cancelled", {"child": value})
        if isinstance(command, ApproveChildImport):
            value = await self._child_manager().approve_import(command.child_id)
            return CommandAcknowledged("imported", {"child": value.model_dump(mode="json")})
        if isinstance(command, RejectChildImport):
            value = await self._child_manager().reject_import(command.child_id)
            return CommandAcknowledged("rejected", {"child": value.model_dump(mode="json")})
        if isinstance(command, CloseChildRun):
            value = await self._child_manager().close_child(command.child_id)
            return CommandAcknowledged("closed", {"child": value.model_dump(mode="json")})
        if isinstance(command, SelectModel):
            return await self._select_model(command)
        if isinstance(command, RetryRun):
            actor = self._actor(command.session_id)
            previous = actor.coordinator.state.last_user_input
            if previous is None:
                raise InvalidStateError("session has no previous input to retry")
            return await self._start_run(
                StartRun(command.session_id, previous, command.client_request_id), is_retry=True
            )
        if isinstance(command, InvokeSkill):
            return await self._invoke_skill(command)
        if isinstance(command, InvokePrompt):
            return await self._invoke_prompt(command)
        if isinstance(command, ListContextSources):
            return self._list_context_sources(command)
        if isinstance(command, ListMcpPrompts):
            return CommandAcknowledged("ok", {"items": self.resources.mcp_prompt_summary()})
        if isinstance(command, ListHooks):
            return CommandAcknowledged("ok", {"items": self.resources.hook_summary()})
        if isinstance(command, SetContextSource):
            return await self._set_context_source(command)
        if isinstance(command, CancelClarification):
            return self._cancel_clarification(command)
        return await self._context_control(command)

    def _bootstrap(self) -> WorkspaceBootstrap:
        summary = self.resources.summary()
        skills = [
            {
                "name": str(getattr(skill, "name", "")),
                "description": str(getattr(skill, "description", "")),
            }
            for skill in self.resources.skills
        ]
        tools = [
            {"name": name, **metadata} for name, metadata in sorted(self.resources.tool_metadata.items())
        ]
        return WorkspaceBootstrap(
            agent=str(summary.get("agent", self.resources.config.agent.name)),
            workspace=str(self.resources.workspace),
            active_model=self.resources.active_model_name(),
            model_id=str(self.resources.active_model_config().id),
            available_models=self.resources.available_models(),
            approval_mode=str(self.resources.config.permissions.default_mode),
            collaboration_mode=self._default_collaboration_mode().value,
            tools=tools,
            mcp=self.resources.mcp_summary(),
            skills=skills,
            warnings=list(self.resources.warnings),
            active_run_id=self._active_run_id,
            live_enabled=self._live_manager is not None,
        )

    def _list_sessions(self) -> SessionList:
        items: list[SessionSummary] = []
        for metadata in self.resources.session_repository.list():
            loaded = self.resources.session_repository.load(metadata.id)
            title = loaded.turns[0].user_input.strip() if loaded.turns else "New session"
            if len(title) > 60:
                title = title[:57].rstrip() + "…"
            items.append(
                SessionSummary(
                    session_id=metadata.id,
                    created_at=metadata.created_at,
                    model_id=metadata.model_id,
                    title=title,
                )
            )
        return SessionList(items)

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        actor = self._actor(session_id)
        state = actor.coordinator.state
        store = TimelineStore(RepositoryTimelineAdapter(self.resources.session_repository, session_id))
        store.load_older(limit=200)
        loaded = self.resources.session_repository.load(session_id)
        pending = loaded.context_state.pending_clarification
        work_state = loaded.work_state
        pending_statuses = {"prepared", "applied", "reconciliation_required"}
        recoverable_statuses = {
            "verified",
            "failed",
            "applied",
            "reconciliation_required",
            "rolled_back",
        }
        return SessionSnapshot(
            session_id=session_id,
            model_id=state.session.model_id,
            created_at=state.session.created_at,
            plan=state.plan.model_dump(mode="json"),
            timeline=store.window(),
            last_user_input=state.last_user_input,
            active_run_id=actor.active_run_id,
            approval_mode=actor.settings.approval_mode,
            collaboration_mode=actor.settings.collaboration_mode.value,
            plan_review_status=actor.settings.plan_review_status.value,
            transcript_density=actor.settings.transcript_density,
            pending_clarification=pending.model_dump(mode="json") if pending is not None else None,
            work_products=[item.model_dump(mode="json") for item in work_state.work_products],
            pending_effects=[
                item.model_dump(mode="json")
                for item in work_state.effects
                if item.status.value in pending_statuses
                or (item.status.value == "failed" and item.after is not None)
            ],
            recoverable_effects=[
                item.model_dump(mode="json")
                for item in work_state.effects
                if item.status.value in recoverable_statuses
                and item.before is not None
                and item.work_product_id is not None
            ],
            agents=[self._agent_view(item) for item in loaded.agent_state.threads],
            agent_usage=(
                self.resources.agent_orchestrator.usage_summary(session_id)
                if getattr(self.resources, "agent_orchestrator", None) is not None
                else {}
            ),
            live_sessions=self._live_session_views(session_id, loaded),
        )

    async def subscribe(self, run_id: str, after_sequence: int | None = None) -> AsyncIterator[EventEnvelope]:
        record = self._run(run_id)
        async for event in record.journal.subscribe(after_sequence):
            yield event

    async def subscribe_live(
        self,
        live_session_id: str,
        after_sequence: int | None = None,
    ) -> AsyncIterator[LiveEvent]:
        manager = self._require_live_manager()
        self.live_snapshot(live_session_id)
        async for event in manager.subscribe(live_session_id, after_sequence):
            yield event

    async def send_live_audio(self, live_session_id: str, audio: bytes) -> None:
        manager = self._require_live_manager()
        self.live_snapshot(live_session_id)
        await manager.send_audio(live_session_id, audio)

    async def subscribe_live_audio(self, live_session_id: str) -> AsyncIterator[bytes]:
        manager = self._require_live_manager()
        self.live_snapshot(live_session_id)
        async for audio in manager.subscribe_audio(live_session_id):
            yield audio

    def live_snapshot(self, live_session_id: str) -> dict[str, Any]:
        manager = self._require_live_manager()
        try:
            state = manager.snapshot(live_session_id)
        except KeyError as error:
            raise RunNotFoundError(str(error)) from error
        self._actor(state.ref.session_id)
        return state.model_dump(mode="json")

    def _create_session(self) -> SessionCreated:
        coordinator = self._coordinator()
        state = coordinator.new_session()
        ui_config = getattr(self.resources.config, "ui", None)
        settings = SessionSettingsState(
            collaboration_mode=self._default_collaboration_mode(),
            approval_mode=str(self.resources.config.permissions.default_mode),
            transcript_density=getattr(ui_config, "transcript_density", "normal"),
        )
        self.resources.session_repository.append_session_settings(state.session.id, settings)
        self._sessions[state.session.id] = _SessionActor(coordinator, state.session, settings)
        return SessionCreated(state.session.id)

    @staticmethod
    def _agent_view(thread: Any) -> dict[str, Any]:
        """Project the durable Agent model into the stable client-facing contract."""

        raw = thread.model_dump(mode="json")
        ref = cast(dict[str, Any], raw.pop("ref"))
        config = cast(dict[str, Any], raw.pop("config"))
        result = cast(dict[str, Any] | None, raw.pop("result"))
        return {
            "id": str(ref["id"]),
            "path": str(ref["path"]),
            "parent_session_id": str(ref["parent_session_id"]),
            "parent_agent_id": str(ref["parent_agent_id"]),
            "root_run_id": str(ref["root_run_id"]),
            "role": str(ref["agent_type"]),
            "status": str(raw["status"]),
            "task": str(raw["task"]),
            "task_name": str(raw["task_name"]),
            "task_generation": int(raw["generation"]),
            "workspace_mode": str(config["workspace_mode"]),
            "model": str(config["model_name"]),
            "plan_step_id": raw["plan_step_id"],
            "criterion_ids": list(raw["criterion_ids"]),
            "created_at": str(raw["created_at"]),
            "updated_at": str(raw["updated_at"]),
            "result_summary": str(result.get("summary", "")) if result is not None else None,
            "result_delivered": bool(result.get("delivered", False)) if result is not None else False,
            "artifact_ref": result.get("artifact_ref") if result is not None else None,
            "worktree": raw["worktree"],
            "branch": raw["branch"],
            "commit": raw["commit"],
            "usage": dict(cast(dict[str, Any], raw["usage"])),
        }

    def _default_collaboration_mode(self) -> CollaborationMode:
        section = getattr(self.resources.config, "collaboration", None)
        return CollaborationMode(str(getattr(section, "default_mode", "default")))

    def _child_manager(self) -> Any:
        manager = getattr(self.resources, "child_run_manager", None)
        if manager is None:
            raise InvalidStateError("child runs are disabled")
        return manager

    def _actor(self, session_id: str) -> _SessionActor:
        actor = self._sessions.get(session_id)
        if actor is not None:
            return actor
        try:
            coordinator = self._coordinator()
            state = coordinator.resume(session_id)
        except FileNotFoundError as error:
            raise SessionNotFoundError(str(error)) from error
        loaded = self.resources.session_repository.load(session_id)
        if self._live_manager is not None:
            self._live_manager.recover_session(session_id)
            loaded = self.resources.session_repository.load(session_id)
        task_workspace = getattr(self.resources, "task_workspace", None)
        if task_workspace is not None:
            task_workspace.bind_session(session_id)
            loaded = self.resources.session_repository.load(session_id)
        actor = _SessionActor(coordinator, state.session, loaded.settings)
        self._sessions[session_id] = actor
        return actor

    def _coordinator(self) -> RunCoordinator:
        return RunCoordinator(
            repository=self.resources.session_repository,
            agent_name=self.resources.config.agent.name,
            model_id=lambda: self.resources.active_model_config().id,
            runtime=lambda: self.resources.runtime,
        )

    async def _start_run(
        self,
        command: StartRun,
        *,
        model_prompt: str | None = None,
        is_retry: bool = False,
        initial_events: tuple[RunEvent, ...] = (),
        completion_revision: int | None = None,
    ) -> RunStartedResult:
        model_prompt = model_prompt if model_prompt is not None else command.model_prompt
        request_key = (command.session_id, command.client_request_id)
        async with self._state_lock:
            previous_id = self._requests.get(request_key)
            if previous_id is not None:
                previous = self._run(previous_id)
                return RunStartedResult(previous.id, previous.session_id, previous.status)
            if self._active_run_id is not None:
                raise WorkspaceBusyError(
                    "another agent run is active",
                    details={"run_id": self._active_run_id},
                )
            if self._live_execution_id is not None:
                raise WorkspaceBusyError(
                    "a Live tool execution is active",
                    details={"live_session_id": self._live_execution_id},
                )
            actor = self._actor(command.session_id)
            run_id = str(uuid4())
            record = _RunRecord(
                id=run_id,
                session_id=command.session_id,
                client_request_id=command.client_request_id,
                journal=EventJournal(session_id=command.session_id, run_id=run_id),
            )
            self._runs[run_id] = record
            self._requests[request_key] = run_id
            self._active_run_id = run_id
            actor.active_run_id = run_id
            for event in initial_events:
                await record.journal.append(event)
            record.task = asyncio.create_task(
                self._execute(
                    actor,
                    record,
                    command.input,
                    model_prompt=model_prompt,
                    is_retry=is_retry,
                    completion_revision=completion_revision,
                ),
                name=f"lumen-run-{run_id}",
            )
        return RunStartedResult(run_id, command.session_id)

    async def _execute(
        self,
        actor: _SessionActor,
        record: _RunRecord,
        text: str,
        *,
        model_prompt: str | None = None,
        is_retry: bool = False,
        completion_revision: int | None = None,
    ) -> None:
        terminal_seen = False
        deferred_completion: RunCompleted | None = None

        async def emit(event: RunEvent) -> None:
            nonlocal terminal_seen, deferred_completion
            if (
                isinstance(event, RunCompleted)
                and actor.settings.collaboration_mode is CollaborationMode.PLAN
            ):
                deferred_completion = event
                return
            terminal_seen = terminal_seen or isinstance(
                event, RunCompleted | RunWaitingForUser | RunFailed | RunCancelled
            )
            await record.journal.append(event)

        async def approve(request: ApprovalRequest) -> ToolApproval:
            decision = self._decide_request(actor, request)
            if not decision.requires_confirmation:
                return ToolApproval(decision.approved, decision.message)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ToolApproval] = loop.create_future()
            record.approvals[request.call_id] = _PendingApproval(request, future)
            await emit(
                ToolApprovalPending(
                    request.call_id,
                    request.name,
                    request.args,
                    request.origin,
                    request.risk,
                )
            )
            return await future

        async def approve_batch(
            requests: tuple[ApprovalRequest, ...],
        ) -> dict[str, ToolApproval]:
            results: dict[str, ToolApproval] = {}
            pending: list[ApprovalRequest] = []
            for request in requests:
                decision = self._decide_request(actor, request)
                if not decision.requires_confirmation:
                    results[request.call_id] = ToolApproval(decision.approved, decision.message)
                    continue
                loop = asyncio.get_running_loop()
                future: asyncio.Future[ToolApproval] = loop.create_future()
                record.approvals[request.call_id] = _PendingApproval(request, future)
                pending.append(request)
            if len(pending) == 1:
                request = pending[0]
                await emit(
                    ToolApprovalPending(
                        request.call_id, request.name, request.args, request.origin, request.risk
                    )
                )
            elif pending:
                risks = Counter(request.risk for request in pending)
                summary = ", ".join(f"{count}x{risk}" for risk, count in sorted(risks.items()))
                await emit(
                    ToolApprovalBatchPending(
                        batch_id=str(uuid4()), requests=tuple(pending), risk_summary=summary
                    )
                )
            if pending:
                decided = await asyncio.gather(
                    *(record.approvals[request.call_id].future for request in pending)
                )
                results.update(
                    (request.call_id, decision) for request, decision in zip(pending, decided, strict=True)
                )
            return results

        prepared_prompt = model_prompt or expand_file_mentions(text, Workspace(self.resources.workspace))
        prepared_prompt = apply_collaboration_context(prepared_prompt, actor.settings.collaboration_mode)
        orchestrator = getattr(self.resources, "agent_orchestrator", None)
        if orchestrator is not None:

            async def emit_agent(agent_event: Any) -> None:
                thread = self.resources.session_repository.load(actor.metadata.id).agent_state.get(
                    agent_event.agent_id
                )
                await emit(
                    AgentLifecycleChanged(
                        agent_id=agent_event.agent_id,
                        path=thread.ref.path if thread is not None else agent_event.agent_id,
                        phase=agent_event.kind.value.removeprefix("agent."),
                        status=agent_event.status.value,
                        summary=str(agent_event.data.get("summary", "")),
                    )
                )

            orchestrator.event_sink = emit_agent
            runtime_factory = getattr(self.resources, "agent_runtime_factory", None)
            if runtime_factory is not None:
                runtime_factory.bind_approval_handler(approve)
            orchestrator.bind_root_run(
                actor.metadata.id,
                record.id,
                approval_mode=actor.settings.approval_mode,
            )
        try:
            outcome = await actor.coordinator.run(
                RunInput(text, prepared_prompt, is_retry=is_retry),
                emit,
                approve,
                approve_batch,
                completion_policy=CompletionPolicy(
                    require_post_mutation_verification=True,
                    max_retries=2,
                    require_reviewable_plan=(actor.settings.collaboration_mode is CollaborationMode.PLAN),
                    required_revision=(
                        completion_revision
                        if completion_revision is not None
                        else (
                            actor.settings.reviewed_revision
                            if actor.settings.plan_review_status is PlanReviewStatus.EXECUTING
                            else None
                        )
                    ),
                ),
            )
            record.status = outcome.status if outcome is not None else "failed"
            if (
                outcome is not None
                and actor.settings.collaboration_mode is CollaborationMode.PLAN
                and outcome.status == "completed"
            ):
                if outcome.plan.is_reviewable():
                    review_plan = outcome.plan.model_copy(
                        update={
                            "lifecycle": PlanLifecycle.REVIEW_PENDING,
                            "state_version": outcome.plan.state_version + 1,
                        }
                    )
                    actor.coordinator.replace_plan(review_plan)
                    self.resources.session_repository.append_plan_state(actor.metadata.id, review_plan)
                    actor.settings = actor.settings.model_copy(
                        update={
                            "plan_review_status": PlanReviewStatus.REVIEW_PENDING,
                            "reviewed_revision": review_plan.revision,
                            "review_client_request_id": None,
                            "execution_run_id": None,
                        }
                    )
                    self.resources.session_repository.append_session_settings(
                        actor.metadata.id, actor.settings
                    )
                    await emit(PlanReviewPending(review_plan, review_plan.revision))
                    if deferred_completion is not None:
                        terminal_seen = True
                        await record.journal.append(deferred_completion)
                    record.status = "review_pending"
                else:
                    await emit(
                        RunFailed("plan_invalid: plan mode requires a non-empty goal and pending steps")
                    )
                    record.status = "failed"
            elif (
                outcome is not None
                and actor.settings.plan_review_status is PlanReviewStatus.EXECUTING
                and outcome.status == "completed"
            ):
                actor.settings = actor.settings.model_copy(
                    update={"plan_review_status": PlanReviewStatus.NONE}
                )
                self.resources.session_repository.append_session_settings(actor.metadata.id, actor.settings)
            if outcome is None and not terminal_seen:
                await emit(RunFailed("run failed"))
        except asyncio.CancelledError:
            record.status = "cancelled"
            if not terminal_seen:
                await emit(RunCancelled())
        except Exception as error:
            record.status = "failed"
            if not terminal_seen:
                await emit(RunFailed(str(error)))
        finally:
            if orchestrator is not None:
                orchestrator.unbind_root_run(record.id)
                orchestrator.event_sink = None
                runtime_factory = getattr(self.resources, "agent_runtime_factory", None)
                if runtime_factory is not None:
                    runtime_factory.bind_approval_handler(None)
            self._resolve_pending(record, approved=False, message="run ended")
            actor.active_run_id = None
            async with self._state_lock:
                if self._active_run_id == record.id:
                    self._active_run_id = None
            await record.journal.close()

    def _run(self, run_id: str) -> _RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise RunNotFoundError(f"run not found: {run_id}") from error

    async def _cancel(self, run_id: str) -> CommandAcknowledged:
        record = self._run(run_id)
        if record.task is None or record.task.done():
            return CommandAcknowledged(record.status)
        self._resolve_pending(record, approved=False, message="run cancelled")
        record.task.cancel()
        try:
            await record.task
        except asyncio.CancelledError:
            pass
        return CommandAcknowledged(record.status)

    def _decide_approval(self, command: DecideApproval) -> CommandAcknowledged:
        record = self._runs.get(command.run_id)
        pending_map = record.approvals if record is not None else self._live_approvals.get(command.run_id)
        if pending_map is None:
            raise RunNotFoundError(f"run or Live session not found: {command.run_id}")
        pending = pending_map.get(command.call_id)
        if pending is None:
            raise ApprovalStateError(f"approval is not pending: {command.call_id}")
        if pending.decision is not None:
            if pending.decision != command.approved:
                raise ApprovalAlreadyResolvedError(
                    f"approval already resolved: {command.call_id}",
                    details={"approved": pending.decision},
                )
            return CommandAcknowledged("approved" if command.approved else "denied")
        owning_session_id = (
            record.session_id
            if record is not None
            else self._require_live_manager().snapshot(command.run_id).ref.session_id
        )
        pending.decision = command.approved
        if command.approved and command.scope == "session":
            actor = self._actor(owning_session_id)
            actor.approval_keys.add(self._approval_scope_key(pending.request))
        action = "allowed" if command.approved else "denied"
        source = "user_session" if command.scope == "session" else "user"
        pending.future.set_result(
            ToolApproval(
                command.approved,
                f"The user {action} this tool call "
                f"(mode={self._actor(owning_session_id).settings.approval_mode}, "
                f"decision_source={source}).",
            )
        )
        return CommandAcknowledged("approved" if command.approved else "denied")

    async def _start_live_session(self, command: StartLiveSession) -> LiveStartedResult:
        manager = self._require_live_manager()
        self._actor(command.session_id)
        result = await manager.connect(
            LiveConnectRequest(
                session_id=command.session_id,
                client_request_id=command.client_request_id,
                sdp=command.sdp,
                route=command.route,
            )
        )
        return LiveStartedResult(
            live_session_id=result.live_session_id,
            session_id=command.session_id,
            media=result.handshake.model_dump(mode="json", exclude_none=True),
            answer_sdp=result.answer_sdp,
            state=result.state.model_dump(mode="json"),
        )

    async def _request_live_approval(
        self,
        session_id: str,
        live_session_id: str,
        request: ApprovalRequest,
    ) -> CapabilityApproval:
        actor = self._actor(session_id)
        decision = self._decide_request(actor, request)
        if not decision.requires_confirmation:
            return CapabilityApproval(decision.approved, decision.message)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolApproval] = loop.create_future()
        pending = _PendingApproval(request, future)
        pending_map = self._live_approvals.setdefault(live_session_id, {})
        pending_map[request.call_id] = pending
        try:
            resolved = await future
            return CapabilityApproval(resolved.approved, resolved.message)
        finally:
            pending_map.pop(request.call_id, None)
            if not pending_map:
                self._live_approvals.pop(live_session_id, None)

    async def _acquire_live_execution(self, session_id: str, live_session_id: str) -> bool:
        self._actor(session_id)
        async with self._state_lock:
            if self._active_run_id is not None or self._live_execution_id is not None:
                return False
            self._live_execution_id = live_session_id
            return True

    async def _release_live_execution(self, live_session_id: str) -> None:
        async with self._state_lock:
            if self._live_execution_id == live_session_id:
                self._live_execution_id = None

    def _require_live_manager(self) -> LiveSessionManager:
        if self._live_manager is None:
            raise InvalidStateError("Realtime voice is disabled")
        return self._live_manager

    def _live_session_views(self, session_id: str, loaded: Any) -> list[dict[str, Any]]:
        states = {item.ref.id: item for item in loaded.live_state.sessions}
        if self._live_manager is not None:
            states.update({item.ref.id: item for item in self._live_manager.states_for(session_id)})
        return [item.model_dump(mode="json") for item in states.values()]

    def _resolve_live_pending(self, live_session_id: str, *, approved: bool, message: str) -> None:
        for pending in self._live_approvals.get(live_session_id, {}).values():
            if pending.decision is None:
                pending.decision = approved
            if not pending.future.done():
                pending.future.set_result(ToolApproval(approved, message))

    async def _queue_input(self, command: QueueRunInput) -> CommandAcknowledged:
        record = self._run(command.run_id)
        if record.status != "running":
            raise InvalidStateError("run is not active")
        actor = self._actor(record.session_id)
        prompt = apply_collaboration_context(
            expand_file_mentions(
                command.model_prompt if command.model_prompt is not None else command.text,
                Workspace(self.resources.workspace),
            ),
            actor.settings.collaboration_mode,
        )
        try:
            message = await actor.coordinator.enqueue_interactive(
                RunInput(command.text, prompt), QueueMode(command.mode)
            )
        except ValueError as error:
            raise InvalidStateError(str(error)) from error
        return CommandAcknowledged("queued", {"message_id": message.id})

    def _set_approval_mode(self, command: SetApprovalMode) -> CommandAcknowledged:
        mode = ApprovalMode.parse(command.mode)
        actor = self._actor(command.session_id)
        if actor.active_run_id is not None:
            raise InvalidStateError("cannot change approval mode while a run is active")
        actor.settings = actor.settings.model_copy(update={"approval_mode": mode.value})
        self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
        return CommandAcknowledged("updated", {"approval_mode": mode.value})

    def _set_collaboration_mode(self, command: SetCollaborationMode) -> CommandAcknowledged:
        actor = self._actor(command.session_id)
        if actor.active_run_id is not None:
            raise InvalidStateError("cannot change collaboration mode while a run is active")
        mode = CollaborationMode(command.mode)
        actor.settings = actor.settings.model_copy(
            update={
                "collaboration_mode": mode,
                "plan_review_status": PlanReviewStatus.NONE,
                "reviewed_revision": None,
                "review_client_request_id": None,
                "execution_run_id": None,
            }
        )
        self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
        return CommandAcknowledged("updated", {"collaboration_mode": mode.value})

    def _decide_request(self, actor: _SessionActor, request: ApprovalRequest) -> Any:
        if actor.settings.collaboration_mode is CollaborationMode.PLAN:
            if ApprovalPolicy.is_read_only(request):
                return ApprovalDecision(
                    approved=True,
                    requires_confirmation=False,
                    source="collaboration_policy",
                    message=f"allowed read in plan mode (tool={request.name})",
                )
            return ApprovalDecision(
                approved=False,
                requires_confirmation=False,
                source="collaboration_policy",
                message=(f"blocked in plan collaboration mode (tool={request.name}, risk={request.risk})"),
            )
        if self._approval_scope_key(request) in actor.approval_keys:
            return ApprovalDecision(
                approved=True,
                requires_confirmation=False,
                source="user_session",
                message=(
                    "auto-approved by the user's session rule "
                    f"(mode={actor.settings.approval_mode}, decision_source=user_session)"
                ),
            )
        return self._approval_policy.decide(request, ApprovalMode.parse(actor.settings.approval_mode))

    @staticmethod
    def _approval_scope_key(request: ApprovalRequest) -> str:
        if request.name == "run_command":
            argv = request.args.get("argv")
            items = cast(list[object], argv) if isinstance(argv, list) else []
            executable = str(items[0]) if items else "<unknown>"
            return f"{request.origin}:{request.name}:{executable}"
        return f"{request.origin}:{request.name}"

    async def _approve_plan(self, command: ApprovePlan) -> RunStartedResult:
        actor = self._actor(command.session_id)
        if actor.settings.review_client_request_id == command.client_request_id:
            if actor.settings.execution_run_id is not None:
                existing = self._runs.get(actor.settings.execution_run_id)
                return RunStartedResult(
                    actor.settings.execution_run_id,
                    command.session_id,
                    existing.status if existing is not None else "running",
                )
            if actor.settings.plan_review_status is not PlanReviewStatus.APPROVED_WAITING_TO_EXECUTE:
                raise InvalidStateError("plan review request was already resolved")
        if (
            actor.settings.plan_review_status is PlanReviewStatus.APPROVED_WAITING_TO_EXECUTE
            and actor.settings.review_client_request_id != command.client_request_id
        ):
            raise InvalidStateError("plan approval is already committed under another request")
        if actor.active_run_id is not None:
            raise WorkspaceBusyError("cannot approve a plan while a run is active")
        if (
            actor.settings.plan_review_status
            not in {
                PlanReviewStatus.REVIEW_PENDING,
                PlanReviewStatus.APPROVED_WAITING_TO_EXECUTE,
            }
            or actor.settings.reviewed_revision != command.revision
            or actor.coordinator.state.plan.revision != command.revision
        ):
            raise InvalidStateError("plan revision is not pending review")
        actor.settings = actor.settings.model_copy(
            update={
                "collaboration_mode": CollaborationMode.DEFAULT,
                "plan_review_status": PlanReviewStatus.APPROVED_WAITING_TO_EXECUTE,
                "review_client_request_id": command.client_request_id,
            }
        )
        self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
        approved_plan = actor.coordinator.state.plan.model_copy(
            update={
                "approved_revision": command.revision,
                "lifecycle": PlanLifecycle.APPROVED,
                "state_version": actor.coordinator.state.plan.state_version + 1,
            }
        )
        actor.coordinator.replace_plan(approved_plan)
        self.resources.session_repository.append_plan_state(command.session_id, approved_plan)
        prompt = (
            f"Execute the approved plan revision {command.revision}. Keep plan steps and evidence "
            "current, verify the result, and do not silently change the approved scope."
        )
        started = await self._start_run(
            StartRun(command.session_id, "Implement the approved plan.", command.client_request_id),
            model_prompt=prompt,
            initial_events=(PlanReviewResolved(command.revision, True),),
            completion_revision=command.revision,
        )
        actor.settings = actor.settings.model_copy(
            update={
                "plan_review_status": PlanReviewStatus.EXECUTING,
                "execution_run_id": started.run_id,
            }
        )
        self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
        return started

    async def _reject_plan(self, command: RejectPlan) -> RunStartedResult:
        actor = self._actor(command.session_id)
        feedback = command.feedback.strip()
        if not feedback:
            raise InvalidStateError("plan rejection requires feedback")
        if (
            actor.settings.review_client_request_id == command.client_request_id
            and actor.settings.execution_run_id is not None
        ):
            existing = self._runs.get(actor.settings.execution_run_id)
            return RunStartedResult(
                actor.settings.execution_run_id,
                command.session_id,
                existing.status if existing is not None else "running",
            )
        if actor.active_run_id is not None:
            raise WorkspaceBusyError("cannot reject a plan while a run is active")
        if (
            actor.settings.plan_review_status is not PlanReviewStatus.REVIEW_PENDING
            or actor.settings.reviewed_revision != command.revision
        ):
            raise InvalidStateError("plan revision is not pending review")
        actor.settings = actor.settings.model_copy(
            update={
                "collaboration_mode": CollaborationMode.PLAN,
                "plan_review_status": PlanReviewStatus.REJECTED,
                "review_client_request_id": command.client_request_id,
            }
        )
        self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
        started = await self._start_run(
            StartRun(command.session_id, feedback, command.client_request_id),
            model_prompt=f"Revise plan revision {command.revision} using this feedback:\n{feedback}",
            initial_events=(PlanReviewResolved(command.revision, False, feedback),),
        )
        actor.settings = actor.settings.model_copy(
            update={
                "plan_review_status": PlanReviewStatus.NONE,
                "reviewed_revision": None,
                "execution_run_id": started.run_id,
            }
        )
        self.resources.session_repository.append_session_settings(command.session_id, actor.settings)
        return started

    def _waive_plan_verification(self, command: WaivePlanVerification) -> CommandAcknowledged:
        actor = self._actor(command.session_id)
        if actor.active_run_id is not None:
            raise WorkspaceBusyError("cannot waive verification while a run is active")
        reason = command.reason.strip()
        if not reason:
            raise InvalidStateError("verification waiver requires a reason")
        plan = actor.coordinator.state.plan
        known_criteria = {criterion.id for step in plan.steps for criterion in step.acceptance_criteria}
        scope = set(command.scope)
        task_workspace = getattr(self.resources, "task_workspace", None)
        known_effects: set[str] = set()
        if task_workspace is not None:
            known_effects = {item.id for item in task_workspace.state_for(command.session_id).effects}
        if not scope <= known_criteria | known_effects:
            raise InvalidStateError("verification waiver scope contains unknown criteria or effects")
        effect_scope = tuple(sorted(scope & known_effects))
        waived_effects = (
            task_workspace.waive_effects(command.session_id, effect_scope, reason)
            if task_workspace is not None and (effect_scope or not scope)
            else 0
        )
        criterion_scope = scope & known_criteria
        if not criterion_scope and scope:
            return CommandAcknowledged("waived", {"effect_count": waived_effects})
        if not scope and plan.approved_revision != plan.revision:
            return CommandAcknowledged("waived", {"effect_count": waived_effects})
        if plan.approved_revision != plan.revision:
            raise InvalidStateError("plan criterion waiver requires the approved plan revision")
        evidence_id = f"e{uuid4().hex[:16]}"
        sequence = max((item.sequence for item in plan.evidence), default=0) + 1
        receipt = EvidenceReceipt(
            id=evidence_id,
            kind=EvidenceKind.USER_WAIVER,
            source_id=f"user:{command.session_id}",
            criterion_ids=sorted(criterion_scope),
            summary=(
                f"User waived verification for {sorted(criterion_scope) or ['post-mutation check']}: {reason}"
            ),
            passed=True,
            sequence=sequence,
        )
        steps = [
            step.model_copy(update={"evidence_ids": list(dict.fromkeys([*step.evidence_ids, evidence_id]))})
            if criterion_scope & {criterion.id for criterion in step.acceptance_criteria}
            else step
            for step in plan.steps
        ]
        updated = plan.model_copy(
            update={
                "steps": steps,
                "evidence": [*plan.evidence, receipt],
                "state_version": plan.state_version + 1,
            }
        )
        actor.coordinator.replace_plan(updated)
        self.resources.session_repository.append_plan_state(command.session_id, updated)
        return CommandAcknowledged(
            "waived",
            {"evidence": receipt.model_dump(mode="json"), "effect_count": waived_effects},
        )

    async def _select_model(self, command: SelectModel) -> CommandAcknowledged:
        if self._active_run_id is not None:
            raise WorkspaceBusyError("cannot switch model while a run is active")
        select_model = getattr(self.resources, "select_model", None)
        if select_model is None:
            raise InvalidStateError("resource manager does not support model switching")
        await select_model(command.model)
        return CommandAcknowledged("updated", {"model": command.model})

    async def _invoke_skill(self, command: InvokeSkill) -> RunStartedResult:
        activate_skill = getattr(self.resources, "activate_skill", None)
        snapshot_activated = activate_skill is not None
        if activate_skill is not None:
            skill = activate_skill(command.session_id, command.name)
        else:
            load_skill = getattr(self.resources, "load_skill_by_name", None)
            skill = load_skill(command.name) if load_skill is not None else None
        if skill is None:
            raise InvalidStateError(f"unknown skill: {command.name}")
        display = f"/skill:{command.name}"
        if command.arguments.strip():
            display = f"{display} {command.arguments.strip()}"
        arguments = command.arguments.strip()
        if snapshot_activated:
            invocation = f"Apply the active Skill `{skill.name}` to this request."
            if arguments:
                invocation = f"{invocation}\n\nUser arguments:\n{arguments}"
        else:
            invocation = expand_skill_for_message(skill, arguments)
        prompt = expand_file_mentions(invocation, Workspace(self.resources.workspace))
        return await self._start_run(
            StartRun(command.session_id, display, command.client_request_id),
            model_prompt=prompt,
        )

    async def _invoke_prompt(self, command: InvokePrompt) -> RunStartedResult:
        self._actor(command.session_id)
        try:
            prompt = await self.resources.render_mcp_prompt(command.reference, command.arguments)
        except Exception as error:
            raise InvalidStateError(f"cannot render MCP prompt: {error}") from error
        return await self._start_run(
            StartRun(command.session_id, command.display_input, command.client_request_id),
            model_prompt=prompt,
        )

    def _list_context_sources(self, command: ListContextSources) -> CommandAcknowledged:
        self._actor(command.session_id)
        return CommandAcknowledged(
            "ok",
            {"items": self.resources.context_source_summary(command.session_id)},
        )

    async def _set_context_source(self, command: SetContextSource) -> CommandAcknowledged:
        self._actor(command.session_id)
        if self._active_run_id is not None:
            raise WorkspaceBusyError("context sources cannot change while a run is active")
        if command.active:
            if command.kind == "skill":
                activate = getattr(self.resources, "activate_skill", None)
                if activate is None:
                    raise InvalidStateError("Skill activation is unavailable")
                activate(command.session_id, command.reference)
            else:
                activate_resource = getattr(self.resources, "activate_mcp_resource", None)
                if activate_resource is None:
                    raise InvalidStateError("MCP resource activation is unavailable")
                await activate_resource(command.session_id, command.reference)
        else:
            deactivate = getattr(self.resources, "deactivate_context_source", None)
            if deactivate is None:
                raise InvalidStateError("context source deactivation is unavailable")
            deactivate(command.session_id, command.kind, command.reference)
        return CommandAcknowledged(
            "updated",
            {"kind": command.kind, "reference": command.reference, "active": command.active},
        )

    def _cancel_clarification(self, command: CancelClarification) -> CommandAcknowledged:
        self._actor(command.session_id)
        manager = getattr(self.resources, "session_context", None)
        if manager is None:
            raise InvalidStateError("clarification state is unavailable")
        manager.set_clarification(command.session_id, None)
        return CommandAcknowledged("cancelled")

    async def _context_control(self, command: ContextControl) -> CommandAcknowledged:
        self._actor(command.session_id)
        runtime = self.resources.runtime
        engine = runtime.context_engine if runtime is not None else None
        if engine is None:
            raise InvalidStateError("context engine is not available")
        if command.control != "report" and self._active_run_id is not None:
            raise WorkspaceBusyError("context cannot be changed while a run is active")
        if command.control == "report":
            context_command: Any = ContextReportCommand(session_id=command.session_id)
        elif command.control == "compact":
            context_command = ContextCompactCommand(
                focus=command.focus,
                session_id=command.session_id,
            )
        else:
            context_command = ContextMemoryCommand(
                action=command.action or "list",
                payload={**command.payload, "session_id": command.session_id},
            )

        async def noop(_event: RunEvent) -> None:
            return None

        result = await engine.control(context_command, noop)
        payload = dict(result.payload)
        if command.control == "report":
            source_summary = getattr(self.resources, "context_source_summary", None)
            if source_summary is not None:
                payload["sources"] = source_summary(command.session_id)
        return CommandAcknowledged(
            result.status,
            {"message": result.message, "payload": payload},
        )

    @staticmethod
    def _resolve_pending(record: _RunRecord, *, approved: bool, message: str) -> None:
        for pending in record.approvals.values():
            if pending.decision is None:
                pending.decision = approved
            if not pending.future.done():
                pending.future.set_result(ToolApproval(approved, message))


__all__ = ["WorkspaceHost", "WorkspaceResources"]
