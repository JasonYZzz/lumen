from __future__ import annotations

import asyncio
import mimetypes
import re
from collections import Counter
from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast, overload
from uuid import uuid4

from lumen.agents.types import ACTIVE_AGENT_STATUSES
from lumen.approval import ApprovalDecision, ApprovalMode, ApprovalPolicy
from lumen.attachments import (
    MAX_ATTACHMENTS_PER_INPUT,
    AttachmentError,
    AttachmentRef,
    AttachmentStore,
)
from lumen.collaboration import (
    CollaborationMode,
    PlanReviewStatus,
    SessionSettingsState,
    apply_collaboration_context,
)
from lumen.config import ModelSettingsConfig
from lumen.configuration import ConfigurationConflictError, ConfigurationEditError
from lumen.context import ContextCompactCommand, ContextMemoryCommand, ContextReportCommand
from lumen.context.memory.redaction import redact_secrets
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
from lumen.files.documents import read_workspace_document
from lumen.interactive_queue import QueueMode
from lumen.live import LiveConnectRequest, LiveEvent
from lumen.live.manager import LiveSessionManager
from lumen.live.types import LiveConnectionState
from lumen.plan import EvidenceKind, EvidenceReceipt, PlanLifecycle
from lumen.reasoning import ReasoningLevel, ReasoningSelection, apply_reasoning, resolve_reasoning
from lumen.run_coordinator import RunCoordinator, RunInput
from lumen.run_diagnostics import build_run_diagnostic
from lumen.runtime import AgentRuntime, CompletionPolicy, ToolApproval
from lumen.sessions import SessionData, SessionMetadata, SessionRepository
from lumen.skills import expand_skill_for_message
from lumen.timeline import RepositoryTimelineAdapter, TimelineStore
from lumen.tools.gateway import CapabilityApproval
from lumen.tools.workspace import Workspace
from lumen.trust import ApprovalRuleStore

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
    ConfigurationConflictHostError,
    ContextControl,
    ContinueAgent,
    CreateSession,
    DecideApproval,
    DeleteModelConfiguration,
    DeleteSession,
    DequeueRunInputs,
    EndLiveSession,
    EventEnvelope,
    ForkSessionAtTurn,
    GetBootstrap,
    GetConfiguration,
    GetInstructions,
    ImportAttachmentPath,
    InspectReasoning,
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
    ListMcpResources,
    ListSessions,
    LiveStartedResult,
    QueueRunInput,
    RejectAgentImport,
    RejectChildImport,
    RejectPlan,
    RenameSession,
    RetryRun,
    RunNotFoundError,
    RunStartedResult,
    SelectModel,
    SelectReasoning,
    SendAgentMessage,
    SessionCreated,
    SessionList,
    SessionNotFoundError,
    SessionSnapshot,
    SessionSummary,
    SetApprovalMode,
    SetCollaborationMode,
    SetContextSource,
    SetMcpServerEnabled,
    SetSessionArchived,
    SetTranscriptDensity,
    StartLiveSession,
    StartRun,
    StoreAttachment,
    UpsertModelConfiguration,
    WaivePlanVerification,
    WorkspaceBootstrap,
    WorkspaceBusyError,
    WorkspaceCommand,
)
from .run_lock import WorkspaceRunLock


class WorkspaceResources(Protocol):
    workspace: Path
    session_repository: SessionRepository
    artifact_store: Any
    runtime: AgentRuntime | None
    config: Any
    tool_metadata: dict[str, dict[str, str]]
    warnings: list[str]
    skills: list[Any]
    agent_orchestrator: Any
    configuration: Any

    async def open(self) -> Any: ...
    async def close(self) -> None: ...
    def active_model_config(self) -> Any: ...
    def active_model_name(self) -> str: ...
    def available_models(self) -> list[str]: ...
    async def apply_model_configuration(
        self,
        agent: Any,
        *,
        active_model_name: str | None = None,
    ) -> None: ...
    def mcp_summary(self) -> list[dict[str, object]]: ...
    def context_source_summary(self, session_id: str) -> list[dict[str, str]]: ...
    def mcp_prompt_summary(self) -> list[dict[str, object]]: ...
    async def render_mcp_prompt(self, reference: str, arguments: dict[str, str]) -> str: ...
    def hook_summary(self) -> list[dict[str, object]]: ...
    def capabilities_report(self) -> dict[str, Any]: ...
    def instructions_report(self) -> dict[str, object]: ...
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
        self._fork_requests: dict[tuple[str, str], tuple[ForkSessionAtTurn, SessionCreated]] = {}
        self._active_run_id: str | None = None
        self._state_lock = asyncio.Lock()
        self._workspace_run_lock = WorkspaceRunLock(resources.workspace)
        self._opened = False
        self._title_tasks: dict[str, asyncio.Task[None]] = {}
        self._title_slots = asyncio.Semaphore(2)
        self._approval_policy = ApprovalPolicy()
        # Production resources own a workspace-scoped rule store; test doubles
        # inject a hermetic one. The fallback keeps the host self-sufficient.
        injected_rules = getattr(resources, "approval_rules", None)
        self._approval_rules: ApprovalRuleStore = (
            injected_rules if injected_rules is not None else ApprovalRuleStore(resources.workspace)
        )
        self._persistent_approval_keys: set[str]
        try:
            self._persistent_approval_keys = self._approval_rules.allowed_keys()
        except OSError:
            # An unreadable rule file must not brick the host; fresh rules can
            # still be written once the underlying I/O recovers.
            self._persistent_approval_keys = set()
        self._live_execution_id: str | None = None
        self._startup_reasoning_applied: set[str] = set()
        self._live_approvals: dict[str, dict[str, _PendingApproval]] = {}
        self._live_manager = getattr(resources, "live_manager", None)
        self._attachment_store = AttachmentStore(resources.artifact_store)
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
            # Recover interrupted metadata work only when no other Host owns
            # an active workspace run. The recorded turn remains the source.
            pending = [
                metadata.id for metadata in self.resources.session_repository.list()
                if (catalog := self.resources.session_repository.load(metadata.id).catalog)
                .title_generation_turn is not None and catalog.deleted_at is None
            ]
            if pending and self._workspace_run_lock.acquire():
                try:
                    for session_id in pending:
                        self._schedule_title(session_id)
                finally:
                    self._workspace_run_lock.release()
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
        title_tasks = list(self._title_tasks.values())
        for task in title_tasks:
            task.cancel()
        await asyncio.gather(*title_tasks, return_exceptions=True)
        if self._opened:
            await self.resources.close()
            self._opened = False
        if self._active_run_id is None and self._live_execution_id is None:
            self._workspace_run_lock.release()

    def capabilities(self) -> dict[str, Any]:
        """Expose the ResourceManager read projection to every client Adapter."""

        return self.resources.capabilities_report()

    async def read_document(self, path: str) -> bytes:
        """Read a workspace document for client preview without changing Session state."""
        try:
            return await asyncio.to_thread(read_workspace_document, self.resources.workspace, path)
        except (OSError, ValueError) as error:
            raise InvalidStateError("Document unavailable: check its path, type and 20 MiB limit") from error

    async def read_attachment(self, value: dict[str, Any]) -> tuple[bytes, str]:
        """Read validated image bytes without exposing arbitrary artifacts to clients."""
        try:
            attachment = AttachmentRef.model_validate(value)
            content = await asyncio.to_thread(self._attachment_store.read, attachment)
        except ValueError as error:
            raise InvalidStateError(str(error)) from error
        return content, attachment.media_type

    @overload
    async def dispatch(self, command: CreateSession) -> SessionCreated: ...

    @overload
    async def dispatch(self, command: ForkSessionAtTurn) -> SessionCreated: ...

    @overload
    async def dispatch(self, command: GetBootstrap) -> WorkspaceBootstrap: ...

    @overload
    async def dispatch(
        self,
        command: (
            GetConfiguration
            | InspectReasoning
            | UpsertModelConfiguration
            | SetMcpServerEnabled
            | DeleteModelConfiguration
        ),
    ) -> CommandAcknowledged: ...

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
            | DeleteSession
            | SetApprovalMode
            | SetCollaborationMode
            | SetTranscriptDensity
            | RenameSession
            | SetSessionArchived
            | SelectModel
            | SelectReasoning
            | ContextControl
            | SetContextSource
            | CancelClarification
            | ListContextSources
            | ListMcpPrompts
            | ListMcpResources
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
            | GetConfiguration
            | GetInstructions
            | InspectReasoning
            | UpsertModelConfiguration
            | SetMcpServerEnabled
            | DeleteModelConfiguration
            | StoreAttachment
            | ImportAttachmentPath
        ),
    ) -> CommandAcknowledged: ...

    async def dispatch(self, command: WorkspaceCommand) -> CommandResult:
        if isinstance(command, GetBootstrap):
            return self._bootstrap()
        if isinstance(command, StoreAttachment):
            try:
                attachment = self._attachment_store.store_image(
                    filename=command.filename,
                    media_type=command.media_type,
                    content=command.content,
                )
            except AttachmentError as error:
                raise InvalidStateError(str(error)) from error
            return CommandAcknowledged(
                "stored",
                {"attachment": attachment.model_dump(mode="json")},
            )
        if isinstance(command, ImportAttachmentPath):
            try:
                target = Workspace(self.resources.workspace).resolve(command.path)
                if not target.is_file():
                    raise AttachmentError(f"image attachment is not a file: {command.path}")
                media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                content = await asyncio.to_thread(target.read_bytes)
                attachment = self._attachment_store.store_image(
                    filename=target.name,
                    media_type=media_type,
                    content=content,
                )
            except (AttachmentError, OSError, ValueError) as error:
                raise InvalidStateError(str(error)) from error
            return CommandAcknowledged(
                "stored",
                {"attachment": attachment.model_dump(mode="json")},
            )
        if isinstance(command, InspectReasoning):
            try:
                config = ModelSettingsConfig.model_validate(command.definition)
                selection = resolve_reasoning(config)
            except ValueError as error:
                raise InvalidStateError(str(error)) from error
            return CommandAcknowledged("ok", {"reasoning": selection.model_dump(mode="json")})
        if isinstance(command, GetConfiguration):
            return CommandAcknowledged(
                "ok",
                {
                    **self.resources.configuration.inspect().as_dict(),
                    "active_model": self.resources.active_model_name(),
                },
            )
        if isinstance(command, GetInstructions):
            return CommandAcknowledged("ok", self.resources.instructions_report())
        if isinstance(command, UpsertModelConfiguration):
            async with self._state_lock:
                self._require_configuration_edit_safe()
                try:
                    snapshot = self.resources.configuration.upsert_model(
                        expected_revision=command.expected_revision,
                        name=command.name,
                        definition=command.definition,
                        set_default=command.set_default,
                    )
                    agent = self.resources.configuration.resolved_agent()
                    registry = agent.model_registry()
                    current = self.resources.active_model_name()
                    active_model = (
                        command.name
                        if command.set_default
                        else current if current in registry else agent.default_model_name()
                    )
                    await self.resources.apply_model_configuration(
                        agent,
                        active_model_name=active_model,
                    )
                except ConfigurationConflictError as error:
                    raise ConfigurationConflictHostError(str(error)) from error
                except ConfigurationEditError as error:
                    raise InvalidStateError(str(error)) from error
            return CommandAcknowledged(
                "saved",
                {
                    **snapshot.as_dict(),
                    "active_model": self.resources.active_model_name(),
                    "restart_required": False,
                },
            )
        if isinstance(command, SetMcpServerEnabled):
            async with self._state_lock:
                self._require_configuration_edit_safe()
                try:
                    snapshot = self.resources.configuration.set_mcp_server_enabled(
                        expected_revision=command.expected_revision,
                        name=command.name,
                        enabled=command.enabled,
                    )
                except ConfigurationConflictError as error:
                    raise ConfigurationConflictHostError(str(error)) from error
                except ConfigurationEditError as error:
                    raise InvalidStateError(str(error)) from error
            return CommandAcknowledged(
                "saved",
                {**snapshot.as_dict(), "restart_required": True},
            )
        if isinstance(command, DeleteModelConfiguration):
            async with self._state_lock:
                self._require_configuration_edit_safe()
                try:
                    snapshot = self.resources.configuration.remove_model(
                        expected_revision=command.expected_revision,
                        name=command.name,
                    )
                    agent = self.resources.configuration.resolved_agent()
                    registry = agent.model_registry()
                    current = self.resources.active_model_name()
                    await self.resources.apply_model_configuration(
                        agent,
                        active_model_name=(
                            current if current in registry else agent.default_model_name()
                        ),
                    )
                except ConfigurationConflictError as error:
                    raise ConfigurationConflictHostError(str(error)) from error
                except ConfigurationEditError as error:
                    raise InvalidStateError(str(error)) from error
            return CommandAcknowledged(
                "saved",
                {
                    **snapshot.as_dict(),
                    "active_model": self.resources.active_model_name(),
                    "restart_required": False,
                },
            )
        if isinstance(command, ListSessions):
            return self._list_sessions(include_archived=command.include_archived)
        if isinstance(command, CreateSession):
            return self._create_session()
        if isinstance(command, RenameSession):
            return self._rename_session(command.session_id, command.title)
        if isinstance(command, SetSessionArchived):
            return self._set_session_archived(command.session_id, archived=command.archived)
        if isinstance(command, DeleteSession):
            return self._delete_session(command.session_id)
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
            async with self._state_lock:
                source = self._managed_session(command.session_id)
                key = (command.session_id, command.client_request_id or "")
                previous = self._fork_requests.get(key) if command.client_request_id else None
                if previous is not None:
                    if previous[0] != command:
                        raise InvalidStateError("fork request ID was already used with different parameters")
                    return previous[1]
                if self._active_run_id is not None or self._live_execution_id is not None:
                    raise WorkspaceBusyError("cannot fork while an agent or Live execution is active")
                if not 0 <= command.through_turn < len(source.turns):
                    raise InvalidStateError("turn index out of range")
                if not self._workspace_run_lock.acquire():
                    raise WorkspaceBusyError("another Lumen process is running in this workspace")
                try:
                    if not command.include_turn:
                        self._check_effect_recovery(command.session_id)
                    created = self.resources.session_repository.fork(
                        command.session_id,
                        through_turn=command.through_turn,
                        include_turn=command.include_turn,
                    )
                finally:
                    self._workspace_run_lock.release()
                result = SessionCreated(created.id)
                if command.client_request_id:
                    self._fork_requests[key] = (command, result)
                return result
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
        if isinstance(command, SelectReasoning):
            return await self._select_reasoning(command)
        if isinstance(command, RetryRun):
            actor = self._actor(command.session_id)
            state = actor.coordinator.state
            previous = state.last_user_input
            if previous is None:
                raise InvalidStateError("session has no previous input to retry")
            loaded = self.resources.session_repository.load(command.session_id)
            if loaded.turns and loaded.turns[-1].status == "running":
                task_workspace = getattr(self.resources, "task_workspace", None)
                completion_issues = getattr(task_workspace, "completion_issues", None)
                if callable(completion_issues):
                    result = cast(Any, completion_issues)(command.session_id)
                    issues = [str(item) for item in cast(list[object], result)]
                    if issues:
                        raise InvalidStateError(
                            "resolve interrupted run effects before retrying",
                            details={"issues": issues},
                        )
            return await self._start_run(
                StartRun(
                    command.session_id,
                    previous,
                    command.client_request_id,
                    attachments=state.last_attachments,
                ),
                is_retry=True,
            )
        if isinstance(command, InvokeSkill):
            return await self._invoke_skill(command)
        if isinstance(command, InvokePrompt):
            return await self._invoke_prompt(command)
        if isinstance(command, ListContextSources):
            return self._list_context_sources(command)
        if isinstance(command, ListMcpPrompts):
            return CommandAcknowledged("ok", {"items": self.resources.mcp_prompt_summary()})
        if isinstance(command, ListMcpResources):
            summary = getattr(self.resources, "mcp_resource_summary", None)
            if summary is None:
                return CommandAcknowledged("ok", {"items": []})
            return CommandAcknowledged("ok", {"items": summary(command.session_id)})
        if isinstance(command, ListHooks):
            return CommandAcknowledged("ok", {"items": self.resources.hook_summary()})
        if isinstance(command, SetContextSource):
            return await self._set_context_source(command)
        if isinstance(command, CancelClarification):
            return self._cancel_clarification(command)
        return await self._context_control(command)

    def _require_configuration_edit_safe(self) -> None:
        if self._active_run_id is not None or self._live_execution_id is not None:
            raise WorkspaceBusyError(
                "cannot edit model configuration while an agent execution is active",
                details={"run_id": self._active_run_id or self._live_execution_id},
            )

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
            input_modalities=list(
                getattr(self.resources.active_model_config(), "input_modalities", ("text",))
            ),
            available_models=self.resources.available_models(),
            approval_mode=str(self.resources.config.permissions.default_mode),
            collaboration_mode=self._default_collaboration_mode().value,
            tools=tools,
            mcp=self.resources.mcp_summary(),
            skills=skills,
            warnings=list(self.resources.warnings),
            active_run_id=self._active_run_id,
            live_enabled=self._live_manager is not None,
            reasoning=self._reasoning_for(),
        )

    def _list_sessions(self, *, include_archived: bool = False) -> SessionList:
        items: list[SessionSummary] = []
        for metadata in self.resources.session_repository.list():
            loaded = self.resources.session_repository.load(metadata.id)
            if loaded.catalog.deleted_at is not None:
                continue
            archived = loaded.catalog.archived_at is not None
            if archived and not include_archived:
                continue
            if not loaded.turns and loaded.catalog.title is None:
                continue
            title = loaded.catalog.title or loaded.turns[0].user_input.strip()
            if len(title) > 60:
                title = title[:57].rstrip() + "…"
            items.append(
                SessionSummary(
                    session_id=metadata.id,
                    created_at=metadata.created_at,
                    model_id=metadata.model_id,
                    title=title,
                    archived=archived,
                    title_pending=loaded.catalog.title_generation_turn is not None,
                )
            )
        return SessionList(items)

    def _rename_session(self, session_id: str, title: str) -> CommandAcknowledged:
        normalized = " ".join(title.split())
        if not normalized:
            raise InvalidStateError("session title cannot be empty")
        if len(normalized) > 80:
            raise InvalidStateError("session title cannot exceed 80 characters")
        loaded = self._managed_session(session_id)
        updated = loaded.catalog.model_copy(update={"title": normalized, "title_generation_turn": None})
        self.resources.session_repository.append_session_catalog(session_id, updated)
        if task := self._title_tasks.get(session_id):
            task.cancel()
        return CommandAcknowledged("renamed", {"title": normalized})

    def _seed_session_title(self, session_id: str, loaded: SessionData) -> None:
        """Publish a placeholder after durable input admission; generation never blocks a run."""

        if loaded.catalog.title is not None:
            return
        updated = loaded.catalog.model_copy(update={
            "title": "新对话", "title_generation_turn": len(loaded.turns),
        })
        self.resources.session_repository.append_session_catalog(session_id, updated)
        self._schedule_title(session_id)

    def _schedule_title(self, session_id: str) -> None:
        if session_id not in self._title_tasks:
            self._title_tasks[session_id] = asyncio.create_task(
                self._generate_title(session_id), name=f"lumen-title-{session_id}",
            )

    async def _generate_title(self, session_id: str) -> None:
        try:
            loaded = self._managed_session(session_id)
            index = loaded.catalog.title_generation_turn
            if index is None or index >= len(loaded.turns):
                return
            source = redact_secrets(loaded.turns[index].user_input)[:2000]
            fallback = " ".join(source.split())
            fallback = fallback[:39].rstrip() + "…" if len(fallback) > 40 else fallback
            title = fallback or "新对话"
            generator = getattr(self.resources, "generate_session_title", None)
            if generator is not None:
                try:
                    async with self._title_slots:
                        result = await asyncio.wait_for(generator(source), timeout=15)
                    result = re.sub(r"<(think|thinking)>.*?</\1>", "", result, flags=re.S | re.I)
                    candidate = " ".join(redact_secrets(result).strip().strip('"\'`“”').split())
                    if candidate and "<" not in candidate and len(candidate) <= 80:
                        title = candidate
                except Exception:
                    # Auxiliary metadata must never fail the user's agent run;
                    # keep a bounded local title and expose no provider errors.
                    pass
            current = self._managed_session(session_id)
            if current.catalog.title_generation_turn == index:
                self.resources.session_repository.append_session_catalog(
                    session_id, current.catalog.model_copy(update={
                        "title": title, "title_generation_turn": None,
                    }),
                )
        except (SessionNotFoundError, OSError):
            pass
        finally:
            self._title_tasks.pop(session_id, None)

    def _set_session_archived(
        self,
        session_id: str,
        *,
        archived: bool,
    ) -> CommandAcknowledged:
        with self._session_management(session_id) as loaded:
            if (loaded.catalog.archived_at is not None) != archived:
                archived_at = datetime.now(UTC).isoformat() if archived else None
                updated = loaded.catalog.model_copy(update={"archived_at": archived_at})
                self.resources.session_repository.append_session_catalog(session_id, updated)
        return CommandAcknowledged("archived" if archived else "restored")

    def _delete_session(self, session_id: str) -> CommandAcknowledged:
        with self._session_management(session_id, include_deleted=True) as loaded:
            if loaded.catalog.deleted_at is None:
                updated = loaded.catalog.model_copy(update={"deleted_at": datetime.now(UTC).isoformat()})
                self.resources.session_repository.append_session_catalog(session_id, updated)
        self._sessions.pop(session_id, None)
        if task := self._title_tasks.get(session_id):
            task.cancel()
        return CommandAcknowledged("deleted")

    def _managed_session(self, session_id: str, *, include_deleted: bool = False) -> SessionData:
        try:
            loaded = self.resources.session_repository.load(session_id)
        except FileNotFoundError as error:
            raise SessionNotFoundError(str(error)) from error
        if loaded.catalog.deleted_at is not None and not include_deleted:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return loaded

    @contextmanager
    def _session_management(
        self, session_id: str, *, include_deleted: bool = False,
    ) -> Generator[SessionData]:
        """Change visibility under the writer lease, never waive completion evidence."""

        # acquire() is reentrant for this instance. Never release the lease
        # owned by an active run or a Live tool execution in the finally block.
        if self._workspace_run_lock.held or self._active_run_id or self._live_execution_id:
            raise WorkspaceBusyError("cannot change Session visibility while the workspace is running")
        if not self._workspace_run_lock.acquire():
            raise WorkspaceBusyError("another Lumen process is running in this workspace")
        try:
            loaded = self._managed_session(session_id, include_deleted=include_deleted)
            self._require_session_management_safe(session_id, loaded)
            yield loaded
        finally:
            self._workspace_run_lock.release()

    def _require_session_management_safe(self, session_id: str, loaded: SessionData) -> None:
        actor = self._sessions.get(session_id)
        issues: list[str] = []
        if actor is not None and actor.active_run_id is not None:
            issues.append("the Session has an active run")
        # Inspect the complete durable Agent projection, not a completion gate
        # filtered to whichever root run happens to be bound to the orchestrator.
        issues.extend(
            f"agent {thread.ref.path} is {thread.status.value}"
            for thread in loaded.agent_state.threads if thread.status in ACTIVE_AGENT_STATUSES
        )
        live_states = {state.ref.id: state for state in loaded.live_state.sessions}
        owned_live = self._live_manager.states_for(session_id) if self._live_manager is not None else ()
        live_states.update({state.ref.id: state for state in owned_live})
        owned_ids = {state.ref.id for state in owned_live}
        issues.extend(
            f"Live session {state.ref.id} is {state.connection.value}; end the connection first"
            for state in live_states.values()
            if state.connection not in {LiveConnectionState.CLOSED, LiveConnectionState.FAILED}
            and (
                state.connection is not LiveConnectionState.RECONCILIATION_REQUIRED
                or state.ref.id in owned_ids
            )
        )
        if issues:
            raise InvalidStateError(
                "cannot change Session visibility while execution is active",
                details={"issues": issues},
            )

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        actor = self._actor(session_id)
        state = actor.coordinator.state
        store = TimelineStore(
            RepositoryTimelineAdapter(
                self.resources.session_repository,
                session_id,
                active_interaction_id=actor.active_run_id,
            )
        )
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
            reasoning=self._reasoning_for(actor),
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
        actor = _SessionActor(coordinator, state.session, settings)
        # Cross-session "always" rules carry over from previous sessions; the
        # project-scoped store is the persistence authority for them.
        actor.approval_keys.update(self._persistent_approval_keys)
        self._sessions[state.session.id] = actor
        self._apply_startup_reasoning(actor)
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
        # Another Host can append a tombstone after this actor was cached.
        # The journal remains authoritative for every public access.
        loaded = self._managed_session(session_id)
        actor = self._sessions.get(session_id)
        if actor is not None:
            return actor
        try:
            coordinator = self._coordinator()
            state = coordinator.resume(session_id)
        except FileNotFoundError as error:
            raise SessionNotFoundError(str(error)) from error
        if self._live_manager is not None:
            self._live_manager.recover_session(session_id)
            loaded = self.resources.session_repository.load(session_id)
        task_workspace = getattr(self.resources, "task_workspace", None)
        if task_workspace is not None:
            task_workspace.bind_session(session_id)
            loaded = self.resources.session_repository.load(session_id)
        actor = _SessionActor(coordinator, state.session, loaded.settings)
        # Cross-session "always" rules carry over from previous sessions; the
        # project-scoped store is the persistence authority for them.
        actor.approval_keys.update(self._persistent_approval_keys)
        self._sessions[session_id] = actor
        self._apply_startup_reasoning(actor)
        return actor

    def _coordinator(self) -> RunCoordinator:
        return RunCoordinator(
            repository=self.resources.session_repository,
            agent_name=self.resources.config.agent.name,
            model_id=lambda: self.resources.active_model_config().id,
            runtime=lambda: self.resources.runtime,
        )

    def _check_effect_recovery(self, session_id: str) -> None:
        """Reject new execution before copying an unresolved external outcome."""

        workspace = getattr(self.resources, "task_workspace", None)
        if workspace is None:
            return
        workspace.bind_session(session_id)
        blockers = [
            str(item) for item in workspace.completion_blockers(session_id)
            if not item.model_recoverable
        ]
        if blockers:
            raise InvalidStateError(
                "此任务有待核实的外部操作。请先在运行详情中处理后再继续或编辑消息。"
                "原对话和产物已保留。重新生成不会解除这些记录。",
                details={"code": "effect_recovery_required", "issues": blockers},
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
        attachments = self._validated_attachments(command.attachments)
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
            loaded = self._managed_session(command.session_id)
            if loaded.catalog.archived_at is not None:
                raise InvalidStateError("restore the archived Session before starting a run")
            if not self._workspace_run_lock.acquire():
                raise WorkspaceBusyError("another Lumen process is running in this workspace")
            try:
                # A read-only Host may have cached this actor while another
                # process held the execution lock. Refresh from the journal
                # after admission so the new writer cannot run from stale state.
                loaded = self._managed_session(command.session_id)
                if loaded.catalog.archived_at is not None:
                    raise InvalidStateError("restore the archived Session before starting a run")
                self._check_effect_recovery(command.session_id)
                if command.regenerate_from_turn is not None:
                    if not 0 <= command.regenerate_from_turn < len(loaded.turns):
                        raise InvalidStateError("turn index out of range")
                    self.resources.session_repository.rewind(
                        command.session_id,
                        before_turn=command.regenerate_from_turn,
                    )
                    runtime = self.resources.runtime
                    if runtime is not None and runtime.context_engine is not None:
                        memory = runtime.context_engine.memory
                        if memory is not None:
                            memory.retract_session_suffix(
                                command.session_id,
                                active_turn_count=command.regenerate_from_turn,
                            )
                        runtime.context_engine.reset_session_projection(command.session_id)
                    loaded = self._managed_session(command.session_id)
                actor = self._sessions.get(command.session_id)
                if actor is None:
                    actor = self._actor(command.session_id)
                else:
                    state = actor.coordinator.resume(command.session_id)
                    actor.metadata = state.session
                    actor.settings = loaded.settings
                selection = self._reasoning_for(actor)
                if selection.requested is not None:
                    selection = self._resolve_reasoning(selection.requested, source=selection.source)
                runtime = self.resources.runtime
                if runtime is not None:
                    runtime.configure_reasoning(selection)
                if actor.settings.reasoning.get(self._reasoning_key()) != selection:
                    self._store_reasoning(actor, selection)
                run_id = str(uuid4())
                record = _RunRecord(
                    id=run_id,
                    session_id=command.session_id,
                    client_request_id=command.client_request_id,
                    journal=EventJournal(session_id=command.session_id, run_id=run_id),
                )
                actor.coordinator.persist_run_start(command.input, run_id, attachments)
                self._seed_session_title(command.session_id, loaded)
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
                        attachments=attachments,
                    ),
                    name=f"lumen-run-{run_id}",
                )
            except BaseException:
                self._workspace_run_lock.release()
                raise
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
        attachments: tuple[AttachmentRef, ...] = (),
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
                summary = str(agent_event.data.get("summary", ""))
                detail = str(agent_event.data.get("detail", "") or "")
                if detail and detail != summary:
                    summary = f"{summary} — {detail}" if summary else detail
                await emit(
                    AgentLifecycleChanged(
                        agent_id=agent_event.agent_id,
                        path=thread.ref.path if thread is not None else agent_event.agent_id,
                        phase=agent_event.kind.value.removeprefix("agent."),
                        status=agent_event.status.value,
                        summary=summary,
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
                RunInput(
                    text,
                    prepared_prompt,
                    is_retry=is_retry,
                    interaction_id=record.id,
                    attachments=attachments,
                ),
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
            actor.coordinator.persist_unhandled_failure(text, record.id, str(error))
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
                    self._workspace_run_lock.release()
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
        if (
            command.approved
            and command.scope in {"session", "always"}
            and not ApprovalPolicy.requires_fresh_confirmation(pending.request.risk)
        ):
            actor = self._actor(owning_session_id)
            key = self._approval_scope_key(pending.request)
            actor.approval_keys.add(key)
            if command.scope == "always":
                self._approval_rules.allow(key)
        action = "allowed" if command.approved else "denied"
        effective_scope = (
            "once"
            if ApprovalPolicy.requires_fresh_confirmation(pending.request.risk)
            else command.scope
        )
        source = {"session": "user_session", "always": "user_always"}.get(
            effective_scope, "user"
        )
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
        if self._managed_session(command.session_id).catalog.archived_at is not None:
            raise InvalidStateError("restore the archived Session before starting Live")
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
            if not self._workspace_run_lock.acquire():
                return False
            self._live_execution_id = live_session_id
            return True

    async def _release_live_execution(self, live_session_id: str) -> None:
        async with self._state_lock:
            if self._live_execution_id == live_session_id:
                self._live_execution_id = None
                self._workspace_run_lock.release()

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
        attachments = self._validated_attachments(command.attachments)
        prompt = apply_collaboration_context(
            expand_file_mentions(
                command.model_prompt if command.model_prompt is not None else command.text,
                Workspace(self.resources.workspace),
            ),
            actor.settings.collaboration_mode,
        )
        try:
            message = await actor.coordinator.enqueue_interactive(
                RunInput(command.text, prompt, attachments=attachments), QueueMode(command.mode)
            )
        except ValueError as error:
            raise InvalidStateError(str(error)) from error
        return CommandAcknowledged("queued", {"message_id": message.id})

    def _validated_attachments(
        self,
        values: tuple[AttachmentRef | dict[str, Any], ...],
    ) -> tuple[AttachmentRef, ...]:
        if len(values) > MAX_ATTACHMENTS_PER_INPUT:
            raise InvalidStateError(
                f"each input supports at most {MAX_ATTACHMENTS_PER_INPUT} image attachments"
            )
        try:
            attachments = tuple(
                item if isinstance(item, AttachmentRef) else AttachmentRef.model_validate(item)
                for item in values
            )
        except ValueError as error:
            raise InvalidStateError(f"invalid attachment: {error}") from error
        if not attachments:
            return ()
        modalities = cast(
            tuple[str, ...],
            tuple(getattr(self.resources.active_model_config(), "input_modalities", ("text",))),
        )
        if "image" not in modalities:
            raise InvalidStateError(
                "the active model does not declare image input support; "
                "set input_modalities: [text, image] only for a verified vision model"
            )
        for attachment in attachments:
            try:
                self._attachment_store.read(attachment)
            except AttachmentError as error:
                raise InvalidStateError(str(error)) from error
        return attachments

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
        if (
            not ApprovalPolicy.requires_fresh_confirmation(request.risk)
            and self._approval_scope_key(request) in actor.approval_keys
        ):
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
            model_prompt=f"根据以下反馈修订计划版本 {command.revision}:\n{feedback}",
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

    def _reasoning_key(self) -> str:
        return f"{self.resources.active_model_name()}:{self.resources.active_model_config().id}"

    def _resolve_reasoning(
        self, effort: ReasoningLevel | None = None, *, source: str = "model",
    ) -> ReasoningSelection:
        config = self.resources.active_model_config()
        if not isinstance(config, ModelSettingsConfig):
            config = ModelSettingsConfig(id=str(config.id))
        return resolve_reasoning(config, effort, source=source)

    def _reasoning_for(self, actor: _SessionActor | None = None) -> ReasoningSelection:
        if actor is not None and (saved := actor.settings.reasoning.get(self._reasoning_key())) is not None:
            current = self._resolve_reasoning()
            # Capabilities are current model facts, not a frozen Session preference.
            return saved.model_copy(update={name: getattr(current, name) for name in (
                "supported_levels", "capability_status", "capability_source", "level_map",
                "catalog_revision", "provider", "capability_documents", "capability_reviewed_on",
                "capability_note", "provider_default_level",
            )})
        startup = getattr(self.resources, "startup_reasoning", None)
        if actor is None and startup is not None:
            return self._resolve_reasoning(startup, source="cli")
        return self._resolve_reasoning()

    def _store_reasoning(self, actor: _SessionActor, selection: ReasoningSelection) -> None:
        settings = actor.settings.model_copy(update={
            "reasoning": {**actor.settings.reasoning, self._reasoning_key(): selection},
        })
        self.resources.session_repository.append_session_settings(actor.metadata.id, settings)
        actor.settings = settings

    def _apply_startup_reasoning(self, actor: _SessionActor) -> None:
        effort = getattr(self.resources, "startup_reasoning", None)
        if effort is not None and actor.metadata.id not in self._startup_reasoning_applied:
            self._store_reasoning(actor, self._resolve_reasoning(effort, source="cli"))
            self._startup_reasoning_applied.add(actor.metadata.id)

    async def _select_reasoning(self, command: SelectReasoning) -> CommandAcknowledged:
        async with self._state_lock:
            if self._active_run_id is not None or self._live_execution_id is not None:
                raise WorkspaceBusyError("cannot change thinking while execution is active")
            if not self._workspace_run_lock.acquire():
                raise WorkspaceBusyError("another Lumen process is running in this workspace")
            try:
                actor = self._actor(command.session_id)
                actor.settings = self._managed_session(command.session_id).settings
                try:
                    selection = self._resolve_reasoning(ReasoningLevel(command.effort), source="session")
                    # Validate conflicting raw controls before persisting a selection.
                    config = self.resources.active_model_config()
                    apply_reasoning(getattr(config, "settings", {}), selection)
                except ValueError as error:
                    raise InvalidStateError(str(error)) from error
                self._store_reasoning(actor, selection)
            finally:
                self._workspace_run_lock.release()
        return CommandAcknowledged("updated", {"reasoning": selection.model_dump(mode="json")})

    async def _select_model(self, command: SelectModel) -> CommandAcknowledged:
        async with self._state_lock:
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
            StartRun(
                command.session_id,
                display,
                command.client_request_id,
                regenerate_from_turn=command.regenerate_from_turn,
            ),
            model_prompt=prompt,
        )

    async def _invoke_prompt(self, command: InvokePrompt) -> RunStartedResult:
        self._actor(command.session_id)
        try:
            prompt = await self.resources.render_mcp_prompt(command.reference, command.arguments)
        except Exception as error:
            raise InvalidStateError(f"cannot render MCP prompt: {error}") from error
        return await self._start_run(
            StartRun(
                command.session_id,
                command.display_input,
                command.client_request_id,
                regenerate_from_turn=command.regenerate_from_turn,
            ),
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
            loaded = self.resources.session_repository.load(command.session_id)
            if loaded.turns:
                payload["latest_run"] = build_run_diagnostic(loaded.turns[-1])
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
