from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, overload
from uuid import uuid4

from lumen.approval import ApprovalMode, ApprovalPolicy
from lumen.context import ContextCompactCommand, ContextMemoryCommand, ContextReportCommand
from lumen.events import (
    ApprovalRequest,
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
from lumen.run_coordinator import RunCoordinator, RunInput
from lumen.runtime import AgentRuntime, ToolApproval
from lumen.sessions import SessionMetadata, SessionRepository
from lumen.skills import expand_skill_for_message
from lumen.timeline import RepositoryTimelineAdapter, TimelineStore
from lumen.tools.workspace import Workspace

from .events import EventJournal
from .models import (
    ApprovalAlreadyResolvedError,
    ApprovalStateError,
    CancelClarification,
    CancelRun,
    CommandAcknowledged,
    CommandResult,
    ContextControl,
    CreateSession,
    DecideApproval,
    EventEnvelope,
    GetBootstrap,
    InvalidStateError,
    InvokePrompt,
    InvokeSkill,
    ListContextSources,
    ListHooks,
    ListMcpPrompts,
    ListSessions,
    QueueRunInput,
    RetryRun,
    RunNotFoundError,
    RunStartedResult,
    SelectModel,
    SessionCreated,
    SessionList,
    SessionNotFoundError,
    SessionSnapshot,
    SessionSummary,
    SetApprovalMode,
    SetContextSource,
    StartRun,
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
    active_run_id: str | None = None


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
        self._approval_mode = ApprovalMode.parse(resources.config.permissions.default_mode)
        self._approval_policy = ApprovalPolicy()

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
        if self._opened:
            await self.resources.close()
            self._opened = False

    @overload
    async def dispatch(self, command: CreateSession) -> SessionCreated: ...

    @overload
    async def dispatch(self, command: GetBootstrap) -> WorkspaceBootstrap: ...

    @overload
    async def dispatch(self, command: ListSessions) -> SessionList: ...

    @overload
    async def dispatch(
        self, command: StartRun | RetryRun | InvokeSkill | InvokePrompt
    ) -> RunStartedResult: ...

    @overload
    async def dispatch(
        self,
        command: (
            CancelRun
            | DecideApproval
            | QueueRunInput
            | SetApprovalMode
            | SelectModel
            | ContextControl
            | SetContextSource
            | CancelClarification
            | ListContextSources
            | ListMcpPrompts
            | ListHooks
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
        if isinstance(command, CancelRun):
            return await self._cancel(command.run_id)
        if isinstance(command, DecideApproval):
            return self._decide_approval(command)
        if isinstance(command, QueueRunInput):
            return await self._queue_input(command)
        if isinstance(command, SetApprovalMode):
            return self._set_approval_mode(command)
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
            approval_mode=self._approval_mode.value,
            tools=tools,
            mcp=self.resources.mcp_summary(),
            skills=skills,
            warnings=list(self.resources.warnings),
            active_run_id=self._active_run_id,
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
        pending = self.resources.session_repository.load(session_id).context_state.pending_clarification
        return SessionSnapshot(
            session_id=session_id,
            model_id=state.session.model_id,
            created_at=state.session.created_at,
            plan=state.plan.model_dump(mode="json"),
            timeline=store.window(),
            last_user_input=state.last_user_input,
            active_run_id=actor.active_run_id,
            pending_clarification=pending.model_dump(mode="json") if pending is not None else None,
        )

    async def subscribe(self, run_id: str, after_sequence: int | None = None) -> AsyncIterator[EventEnvelope]:
        record = self._run(run_id)
        async for event in record.journal.subscribe(after_sequence):
            yield event

    def _create_session(self) -> SessionCreated:
        coordinator = self._coordinator()
        state = coordinator.new_session()
        self._sessions[state.session.id] = _SessionActor(coordinator, state.session)
        return SessionCreated(state.session.id)

    def _actor(self, session_id: str) -> _SessionActor:
        actor = self._sessions.get(session_id)
        if actor is not None:
            return actor
        try:
            coordinator = self._coordinator()
            state = coordinator.resume(session_id)
        except FileNotFoundError as error:
            raise SessionNotFoundError(str(error)) from error
        actor = _SessionActor(coordinator, state.session)
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
    ) -> RunStartedResult:
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
            record.task = asyncio.create_task(
                self._execute(
                    actor,
                    record,
                    command.input,
                    model_prompt=model_prompt,
                    is_retry=is_retry,
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
    ) -> None:
        terminal_seen = False

        async def emit(event: RunEvent) -> None:
            nonlocal terminal_seen
            terminal_seen = terminal_seen or isinstance(
                event, RunCompleted | RunWaitingForUser | RunFailed | RunCancelled
            )
            await record.journal.append(event)

        async def approve(request: ApprovalRequest) -> ToolApproval:
            decision = self._approval_policy.decide(request, self._approval_mode)
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
                decision = self._approval_policy.decide(request, self._approval_mode)
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
        try:
            outcome = await actor.coordinator.run(
                RunInput(text, prepared_prompt, is_retry=is_retry), emit, approve, approve_batch
            )
            record.status = outcome.status if outcome is not None else "failed"
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
        record = self._run(command.run_id)
        pending = record.approvals.get(command.call_id)
        if pending is None:
            raise ApprovalStateError(f"approval is not pending: {command.call_id}")
        if pending.decision is not None:
            if pending.decision != command.approved:
                raise ApprovalAlreadyResolvedError(
                    f"approval already resolved: {command.call_id}",
                    details={"approved": pending.decision},
                )
            return CommandAcknowledged("approved" if command.approved else "denied")
        pending.decision = command.approved
        action = "allowed" if command.approved else "denied"
        pending.future.set_result(
            ToolApproval(
                command.approved,
                f"The user {action} this tool call (mode={self._approval_mode.value}, decision_source=user).",
            )
        )
        return CommandAcknowledged("approved" if command.approved else "denied")

    async def _queue_input(self, command: QueueRunInput) -> CommandAcknowledged:
        record = self._run(command.run_id)
        if record.status != "running":
            raise InvalidStateError("run is not active")
        actor = self._actor(record.session_id)
        prompt = expand_file_mentions(command.text, Workspace(self.resources.workspace))
        try:
            message = await actor.coordinator.enqueue_interactive(
                RunInput(command.text, prompt), QueueMode(command.mode)
            )
        except ValueError as error:
            raise InvalidStateError(str(error)) from error
        return CommandAcknowledged("queued", {"message_id": message.id})

    def _set_approval_mode(self, command: SetApprovalMode) -> CommandAcknowledged:
        mode = ApprovalMode.parse(command.mode)
        self._approval_mode = mode
        return CommandAcknowledged("updated", {"approval_mode": mode.value})

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
