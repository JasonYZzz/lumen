"""Session-scoped control plane for native multi-agent execution."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from lumen.config import AgentsConfig
from lumen.context.artifacts import ArtifactStore
from lumen.plan import EvidenceKind, EvidenceReceipt, PlanState
from lumen.sessions import SessionRepository

from .types import (
    ACTIVE_AGENT_STATUSES,
    AgentConfigSnapshot,
    AgentEvent,
    AgentEventKind,
    AgentExecutionResult,
    AgentMessage,
    AgentProfile,
    AgentResult,
    AgentStatus,
    AgentThreadRef,
    AgentThreadState,
    SessionAgentState,
    WorkspaceMode,
    utc_now,
)


class AgentRuntimeFactory(Protocol):
    """Internal Seam between orchestration and one child Agent Runtime."""

    def snapshot(self, profile: AgentProfile, *, approval_mode: str) -> AgentConfigSnapshot: ...

    async def execute(
        self,
        thread: AgentThreadState,
        profile: AgentProfile,
        messages: Sequence[AgentMessage],
    ) -> AgentExecutionResult: ...

    async def import_changes(self, thread: AgentThreadState) -> AgentExecutionResult: ...

    async def reject_changes(self, thread: AgentThreadState) -> None: ...

    async def close(self, thread: AgentThreadState) -> None: ...

    def queue_message(self, thread: AgentThreadState, message: str) -> bool: ...


AgentEventSink = Callable[[AgentEvent], Awaitable[None] | None]
EvidenceSink = Callable[[EvidenceReceipt, str | None, tuple[str, ...]], Awaitable[None] | None]
PlanProvider = Callable[[], PlanState]


class AgentOrchestrator:
    """Own scheduling, lifecycle, persistence, messaging, and completion policy.

    Callers bind the current root run once, then use the compact model-tool
    Interface. All state transitions are appended through SessionRepository.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: AgentsConfig,
        profiles: dict[str, AgentProfile],
        repository: SessionRepository,
        artifacts: ArtifactStore,
        runtime_factory: AgentRuntimeFactory,
        plan_provider: PlanProvider | None = None,
        evidence_sink: EvidenceSink | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config
        self.profiles = dict(profiles)
        self.repository = repository
        self.artifacts = artifacts
        self.runtime_factory = runtime_factory
        self.plan_provider = plan_provider
        self.evidence_sink = evidence_sink
        self.event_sink = event_sink
        self._session_id: str | None = None
        self._root_run_id: str | None = None
        self._approval_mode = "manual"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._condition = asyncio.Condition()
        self._event_lock = asyncio.Lock()
        self._recovered_sessions: set[str] = set()
        bind_status = getattr(runtime_factory, "bind_status_handler", None)
        if bind_status is not None:
            bind_status(self._runtime_status_changed)

    def bind_root_run(self, session_id: str, root_run_id: str, *, approval_mode: str) -> None:
        """Bind model tools to one owning Session and root run."""

        self.repository.load(session_id)
        self._session_id = session_id
        self._root_run_id = root_run_id
        self._approval_mode = approval_mode
        if session_id not in self._recovered_sessions:
            self._recover_session(session_id)
            self._recovered_sessions.add(session_id)

    def unbind_root_run(self, root_run_id: str) -> None:
        if self._root_run_id == root_run_id:
            self._root_run_id = None

    async def spawn_agent(
        self,
        task: str,
        agent_type: str | None = None,
        task_name: str | None = None,
        plan_step_id: str | None = None,
        criterion_ids: list[str] | None = None,
    ) -> str:
        session_id, root_run_id = self._bound_context()
        prompt = task.strip()
        if not prompt:
            raise ValueError("agent task must not be empty")
        if not self.config.enabled or self.config.autonomy == "disabled":
            raise ValueError("multi-agent execution is disabled")
        selected_type = agent_type or self.config.default_agent
        try:
            profile = self.profiles[selected_type]
        except KeyError as error:
            raise ValueError(
                f"unknown agent_type {selected_type!r}; available: {sorted(self.profiles)}"
            ) from error
        criteria = tuple(dict.fromkeys(criterion_ids or []))
        self._validate_plan_targets(plan_step_id, criteria)
        state = self.repository.load(session_id).agent_state
        current_run = [item for item in state.threads if item.ref.root_run_id == root_run_id]
        if len(current_run) >= self.config.max_agents_per_run:
            raise ValueError(
                f"agent limit reached for root run ({self.config.max_agents_per_run})"
            )
        requested_name = task_name or self._default_task_name(prompt)
        idempotency_key = self._idempotency_key(
            session_id,
            root_run_id,
            selected_type,
            requested_name,
            prompt,
        )
        existing = next(
            (item for item in state.threads if item.idempotency_key == idempotency_key),
            None,
        )
        if existing is not None:
            return self._json(existing)
        normalized_name = self._unique_task_name(state, requested_name)
        agent_id = f"agent-{uuid4().hex[:16]}"
        snapshot = self.runtime_factory.snapshot(profile, approval_mode=self._approval_mode)
        dirty_hash_provider = getattr(self.runtime_factory, "parent_dirty_hash", None)
        parent_dirty_hash = (
            dirty_hash_provider(self.workspace)
            if profile.workspace_mode is WorkspaceMode.WORKTREE
            and dirty_hash_provider is not None
            else None
        )
        thread = AgentThreadState(
            ref=AgentThreadRef(
                id=agent_id,
                path=f"/root/{normalized_name}",
                parent_session_id=session_id,
                root_run_id=root_run_id,
                agent_type=profile.name,
            ),
            task=prompt,
            task_name=normalized_name,
            config=snapshot,
            plan_step_id=plan_step_id,
            criterion_ids=criteria,
            idempotency_key=idempotency_key,
            parent_dirty_hash=parent_dirty_hash,
        )
        self.repository.append_agent_thread(session_id, thread)
        await self._emit(thread, AgentEventKind.SPAWNED)
        self._schedule(thread)
        return self._json(thread)

    async def send_message(self, agent_id: str, message: str) -> str:
        thread = self._owned_thread(agent_id)
        content = message.strip()
        if not content:
            raise ValueError("agent message must not be empty")
        artifact_ref = self.artifacts.spill(content)
        durable_content = content if artifact_ref is None else content[:1200]
        item = AgentMessage(
            id=f"msg-{uuid4().hex[:16]}",
            agent_id=agent_id,
            sender="/root",
            content=durable_content,
            artifact_ref=artifact_ref,
            trigger_turn=False,
        )
        self.repository.append_agent_message(thread.ref.parent_session_id, item)
        queue_message = getattr(self.runtime_factory, "queue_message", None)
        delivered_to_active = False
        if queue_message is not None and thread.status in {
            AgentStatus.RUNNING,
            AgentStatus.APPROVAL_PENDING,
        }:
            # The scheduler marks a thread running immediately before the
            # factory publishes its runtime handle. Yield once to close that
            # narrow hand-off race; the durable message remains the fallback.
            delivered_to_active = bool(queue_message(thread, content))
            if not delivered_to_active:
                await asyncio.sleep(0)
                delivered_to_active = bool(queue_message(self._owned_thread(agent_id), content))
        await self._emit(thread, AgentEventKind.MESSAGE, {"message_id": item.id})
        return json.dumps(
            {
                **item.model_dump(mode="json"),
                "delivered_to_active": delivered_to_active,
            },
            ensure_ascii=False,
        )

    async def followup_task(self, agent_id: str, task: str) -> str:
        thread = self._owned_thread(agent_id)
        if thread.status in ACTIVE_AGENT_STATUSES and thread.status is not AgentStatus.WAITING:
            raise ValueError("agent already has an active task")
        if thread.status in {
            AgentStatus.IMPORT_PENDING,
            AgentStatus.RECONCILIATION_REQUIRED,
            AgentStatus.CLOSED,
        }:
            raise ValueError(f"agent cannot continue while status is {thread.status.value}")
        prompt = task.strip()
        if not prompt:
            raise ValueError("follow-up task must not be empty")
        message = AgentMessage(
            id=f"msg-{uuid4().hex[:16]}",
            agent_id=agent_id,
            sender="/root",
            content=prompt,
            trigger_turn=True,
        )
        self.repository.append_agent_message(thread.ref.parent_session_id, message)
        updated = thread.model_copy(
            update={
                "task": prompt,
                "generation": thread.generation + 1,
                "status": AgentStatus.QUEUED,
                "result": None,
                "resolution": None,
                "resolution_reason": None,
                "updated_at": utc_now(),
            }
        )
        self.repository.append_agent_thread(thread.ref.parent_session_id, updated)
        await self._emit(updated, AgentEventKind.MESSAGE, {"message_id": message.id})
        self._schedule(updated)
        return self._json(updated)

    async def wait_agent(self, agent_ids: list[str], timeout_ms: int = 30_000) -> str:
        if not agent_ids:
            raise ValueError("agent_ids must not be empty")
        timeout = max(0.0, min(float(timeout_ms), 300_000.0) / 1000.0)
        targets = tuple(dict.fromkeys(agent_ids))

        def changed() -> bool:
            return any(
                self._owned_thread(agent_id).status
                not in {AgentStatus.QUEUED, AgentStatus.RUNNING}
                for agent_id in targets
            )

        if not changed() and timeout > 0:
            try:
                async with asyncio.timeout(timeout):
                    async with self._condition:
                        await self._condition.wait_for(changed)
            except TimeoutError:
                pass
        threads = [self._mark_delivered(self._owned_thread(agent_id)) for agent_id in targets]
        return json.dumps(
            {"timed_out": not changed(), "agents": [item.model_dump(mode="json") for item in threads]},
            ensure_ascii=False,
        )

    async def interrupt_agent(self, agent_id: str) -> str:
        self._owned_thread(agent_id)
        task = self._tasks.get(agent_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        latest = self._owned_thread(agent_id)
        if latest.status in ACTIVE_AGENT_STATUSES:
            latest = await self._transition(latest, AgentStatus.INTERRUPTED, AgentEventKind.INTERRUPTED)
        return self._json(latest)

    async def list_agents(self, path_prefix: str | None = None) -> str:
        session_id, _root_run_id = self._bound_context()
        threads = list(self.repository.load(session_id).agent_state.threads)
        if path_prefix:
            threads = [item for item in threads if item.ref.path.startswith(path_prefix)]
        return json.dumps([item.model_dump(mode="json") for item in threads], ensure_ascii=False)

    async def close_agent(
        self,
        agent_id: str,
        resolution: str | None = None,
        reason: str | None = None,
    ) -> str:
        thread = self._owned_thread(agent_id)
        if thread.status in ACTIVE_AGENT_STATUSES:
            await self.interrupt_agent(agent_id)
            thread = self._owned_thread(agent_id)
        if thread.status is AgentStatus.IMPORT_PENDING:
            raise ValueError("import_pending agent must be imported or rejected before close")
        if thread.status in {
            AgentStatus.FAILED,
            AgentStatus.INTERRUPTED,
            AgentStatus.RECONCILIATION_REQUIRED,
        } and (not resolution or not reason or not reason.strip()):
            raise ValueError("unresolved agent requires resolution and reason before close")
        await self.runtime_factory.close(thread)
        updated = thread.model_copy(
            update={
                "status": AgentStatus.CLOSED,
                "resolution": resolution or thread.resolution,
                "resolution_reason": reason.strip() if reason else thread.resolution_reason,
                "updated_at": utc_now(),
                "result": (
                    thread.result.model_copy(update={"delivered": True})
                    if thread.result is not None
                    else None
                ),
            }
        )
        if updated.result is not None and updated.result != thread.result:
            self.repository.append_agent_result(thread.ref.parent_session_id, updated.result)
        self.repository.append_agent_thread(thread.ref.parent_session_id, updated)
        await self._emit(updated, AgentEventKind.CLOSED)
        return self._json(updated)

    async def approve_import(self, agent_id: str) -> AgentThreadState:
        thread = self._owned_thread(agent_id)
        if thread.status is not AgentStatus.IMPORT_PENDING:
            raise ValueError("agent is not pending import")
        result = await self.runtime_factory.import_changes(thread)
        status = result.status
        if status not in {AgentStatus.IMPORTED, AgentStatus.RECONCILIATION_REQUIRED}:
            raise ValueError(f"invalid import result status: {status.value}")
        imported_result = self._result(thread, result, delivered=True)
        updated = thread.model_copy(
            update={
                "status": status,
                "updated_at": utc_now(),
                "result": imported_result,
            }
        )
        self.repository.append_agent_result(thread.ref.parent_session_id, imported_result)
        self.repository.append_agent_thread(thread.ref.parent_session_id, updated)
        await self._emit(
            updated,
            AgentEventKind.IMPORTED
            if status is AgentStatus.IMPORTED
            else AgentEventKind.RECONCILIATION_REQUIRED,
        )
        await self._record_evidence(updated)
        return updated

    async def reject_import(self, agent_id: str) -> AgentThreadState:
        thread = self._owned_thread(agent_id)
        if thread.status is not AgentStatus.IMPORT_PENDING:
            raise ValueError("agent is not pending import")
        await self.runtime_factory.reject_changes(thread)
        updated = await self._transition(thread, AgentStatus.REJECTED, AgentEventKind.REJECTED)
        return updated

    async def shutdown(self) -> None:
        for task in tuple(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in tuple(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def completion_issues(self, session_id: str) -> list[str]:
        try:
            state = self.repository.load(session_id).agent_state
        except (FileNotFoundError, ValueError):
            return []
        root_run_id = self._root_run_id
        threads = [
            item
            for item in state.threads
            if root_run_id is None or item.ref.root_run_id == root_run_id
        ]
        issues: list[str] = []
        for thread in threads:
            label = thread.ref.path
            if thread.status in ACTIVE_AGENT_STATUSES:
                issues.append(f"agent_active: {label} is {thread.status.value}")
            elif thread.status is AgentStatus.IMPORT_PENDING:
                issues.append(f"agent_import_pending: {label}")
            elif thread.status is AgentStatus.RECONCILIATION_REQUIRED and not thread.resolution:
                issues.append(f"agent_reconciliation_required: {label}")
            elif thread.status in {AgentStatus.FAILED, AgentStatus.INTERRUPTED} and not thread.resolution:
                issues.append(f"agent_unresolved: {label} is {thread.status.value}")
            elif thread.result is not None and not thread.result.delivered:
                issues.append(f"agent_result_undelivered: {label}")
        return issues

    def context_documents(self, session_id: str) -> tuple[dict[str, object], ...]:
        state = self.repository.load(session_id).agent_state
        documents: list[dict[str, object]] = []
        for thread in state.threads[-self.config.max_agents_per_run :]:
            documents.append(
                {
                    "server": "agent-runtime",
                    "uri": thread.ref.path,
                    "revision": thread.updated_at,
                    "body": json.dumps(
                        {
                            "agent_id": thread.ref.id,
                            "status": thread.status.value,
                            "task": thread.task,
                            "result": thread.result.summary if thread.result else None,
                            "artifact_ref": thread.result.artifact_ref if thread.result else None,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        return tuple(documents)

    def usage_summary(
        self,
        session_id: str,
        root_run_id: str | None = None,
    ) -> dict[str, object]:
        """Return per-Agent and aggregate numeric usage for one root run."""

        threads = self.repository.load(session_id).agent_state.threads
        selected = [
            item
            for item in threads
            if root_run_id is None or item.ref.root_run_id == root_run_id
        ]
        total: dict[str, int | float] = {}
        by_agent: dict[str, dict[str, object]] = {}
        for thread in selected:
            by_agent[thread.ref.id] = {
                "path": thread.ref.path,
                "generation": thread.generation,
                "usage": dict(thread.usage),
            }
            for key, value in thread.usage.items():
                if isinstance(value, bool) or not isinstance(value, int | float):
                    continue
                total[key] = total.get(key, 0) + value
        return {"total": total, "by_agent": by_agent}

    @property
    def bound_root_run_id(self) -> str | None:
        return self._root_run_id

    def list(self, session_id: str) -> list[AgentThreadState]:
        return list(self.repository.load(session_id).agent_state.threads)

    def get_agent(self, agent_id: str) -> AgentThreadState:
        """Return one Agent through the ownership-checked compatibility seam."""

        return self._owned_thread(agent_id)

    def _schedule(self, thread: AgentThreadState) -> None:
        existing = self._tasks.get(thread.ref.id)
        if existing is not None and not existing.done():
            return
        self._tasks[thread.ref.id] = asyncio.create_task(
            self._execute(thread.ref.id),
            name=f"lumen-{thread.ref.id}-g{thread.generation}",
        )

    async def _execute(self, agent_id: str) -> None:
        thread = self._owned_thread(agent_id)
        semaphore = self._semaphores.setdefault(
            thread.ref.parent_session_id,
            asyncio.Semaphore(self.config.max_concurrency),
        )
        try:
            async with semaphore:
                thread = await self._transition(
                    thread,
                    AgentStatus.RUNNING,
                    AgentEventKind.STARTED,
                )
                profile = self.profiles[thread.ref.agent_type]
                state = self.repository.load(thread.ref.parent_session_id).agent_state
                messages = [item for item in state.messages if item.agent_id == agent_id]
                async with asyncio.timeout(thread.config.timeout_seconds):
                    execution = await self.runtime_factory.execute(thread, profile, messages)
                artifact_ref = self.artifacts.store(execution.output) if execution.output else None
                transcript_ref = (
                    self.artifacts.store(execution.transcript) if execution.transcript else None
                )
                status = execution.status
                result = self._result(
                    thread,
                    execution,
                    artifact_ref=artifact_ref,
                    transcript_ref=transcript_ref,
                )
                updated = thread.model_copy(
                    update={
                        "status": status,
                        "updated_at": utc_now(),
                        "result": result,
                        "worktree": execution.worktree,
                        "branch": execution.branch,
                        "base_commit": execution.base_commit,
                        "commit": execution.commit,
                        "diff_artifact_ref": (
                            self.artifacts.store(execution.diff) if execution.diff else None
                        ),
                        "history_ref": transcript_ref or thread.history_ref,
                        "usage": self._merge_usage(thread.usage, execution.usage),
                    }
                )
                self.repository.append_agent_result(thread.ref.parent_session_id, result)
                self.repository.append_agent_thread(thread.ref.parent_session_id, updated)
                event_kind = {
                    AgentStatus.COMPLETED: AgentEventKind.COMPLETED,
                    AgentStatus.IMPORT_PENDING: AgentEventKind.IMPORT_PENDING,
                    AgentStatus.RECONCILIATION_REQUIRED: AgentEventKind.RECONCILIATION_REQUIRED,
                }.get(status, AgentEventKind.FAILED)
                await self._emit(updated, event_kind)
                await self._record_evidence(updated)
        except asyncio.CancelledError:
            latest = self._owned_thread(agent_id)
            if latest.status in ACTIVE_AGENT_STATUSES:
                await self._transition(
                    latest,
                    AgentStatus.INTERRUPTED,
                    AgentEventKind.INTERRUPTED,
                )
            raise
        except Exception as error:
            latest = self._owned_thread(agent_id)
            execution = AgentExecutionResult(
                status=AgentStatus.FAILED,
                error=str(error),
                output="",
            )
            result = self._result(latest, execution)
            updated = latest.model_copy(
                update={"status": AgentStatus.FAILED, "result": result, "updated_at": utc_now()}
            )
            self.repository.append_agent_result(latest.ref.parent_session_id, result)
            self.repository.append_agent_thread(latest.ref.parent_session_id, updated)
            await self._emit(updated, AgentEventKind.FAILED, {"error": str(error)})
            await self._record_evidence(updated)
        finally:
            async with self._condition:
                self._condition.notify_all()

    async def _record_evidence(self, thread: AgentThreadState) -> None:
        # Evidence creation may race with a waiter marking the result delivered.
        # Re-read the append-only projection so enrichment never resurrects a
        # stale ``delivered=False`` result.
        latest = (
            self.repository.load(thread.ref.parent_session_id).agent_state.get(thread.ref.id)
            or thread
        )
        result = latest.result
        if result is None:
            return
        passed = latest.status in {
            AgentStatus.COMPLETED,
            AgentStatus.IMPORT_PENDING,
            AgentStatus.IMPORTED,
        }
        receipt = EvidenceReceipt(
            id=f"e{uuid4().hex[:16]}",
            kind=EvidenceKind.AGENT,
            source_id=latest.ref.id,
            criterion_ids=list(latest.criterion_ids),
            summary=(result.summary or result.error or latest.status.value)[:1000],
            passed=passed,
        )
        enriched_result = result.model_copy(update={"evidence": (*result.evidence, receipt)})
        enriched_thread = latest.model_copy(
            update={"result": enriched_result, "updated_at": utc_now()}
        )
        self.repository.append_agent_result(latest.ref.parent_session_id, enriched_result)
        self.repository.append_agent_thread(latest.ref.parent_session_id, enriched_thread)
        if self.evidence_sink is not None:
            response = self.evidence_sink(receipt, latest.plan_step_id, latest.criterion_ids)
            if inspect.isawaitable(response):
                await response

    def _result(
        self,
        thread: AgentThreadState,
        execution: AgentExecutionResult,
        *,
        artifact_ref: str | None = None,
        transcript_ref: str | None = None,
        delivered: bool = False,
    ) -> AgentResult:
        summary = execution.output.strip()
        if len(summary) > 1000:
            summary = summary[:997] + "..."
        return AgentResult(
            agent_id=thread.ref.id,
            generation=thread.generation,
            status=execution.status,
            summary=summary,
            artifact_ref=artifact_ref,
            transcript_ref=transcript_ref,
            error=execution.error,
            usage=execution.usage,
            effect_receipts=execution.effect_receipts,
            delivered=delivered,
        )

    async def _transition(
        self,
        thread: AgentThreadState,
        status: AgentStatus,
        kind: AgentEventKind,
    ) -> AgentThreadState:
        updated = thread.model_copy(update={"status": status, "updated_at": utc_now()})
        self.repository.append_agent_thread(thread.ref.parent_session_id, updated)
        await self._emit(updated, kind)
        async with self._condition:
            self._condition.notify_all()
        return updated

    async def _runtime_status_changed(self, agent_id: str, status: AgentStatus) -> None:
        thread = self._owned_thread(agent_id)
        if thread.status not in ACTIVE_AGENT_STATUSES:
            return
        await self._transition(
            thread,
            status,
            AgentEventKind.APPROVAL_REQUESTED
            if status is AgentStatus.APPROVAL_PENDING
            else AgentEventKind.STARTED,
        )

    async def _emit(
        self,
        thread: AgentThreadState,
        kind: AgentEventKind,
        data: dict[str, object] | None = None,
    ) -> None:
        async with self._event_lock:
            state = self.repository.load(thread.ref.parent_session_id).agent_state
            event = AgentEvent(
                id=f"aevt-{uuid4().hex[:16]}",
                sequence=len(state.events) + 1,
                agent_id=thread.ref.id,
                session_id=thread.ref.parent_session_id,
                root_run_id=thread.ref.root_run_id,
                kind=kind,
                status=thread.status,
                data=dict(data or {}),
            )
            self.repository.append_agent_event(thread.ref.parent_session_id, event)
        if self.event_sink is not None:
            response = self.event_sink(event)
            if inspect.isawaitable(response):
                await response

    def _mark_delivered(self, thread: AgentThreadState) -> AgentThreadState:
        if thread.result is None or thread.result.delivered:
            return thread
        result = thread.result.model_copy(update={"delivered": True})
        updated = thread.model_copy(update={"result": result, "updated_at": utc_now()})
        self.repository.append_agent_result(thread.ref.parent_session_id, result)
        self.repository.append_agent_thread(thread.ref.parent_session_id, updated)
        return updated

    def _owned_thread(self, agent_id: str) -> AgentThreadState:
        if self._session_id is not None:
            thread = self.repository.load(self._session_id).agent_state.get(agent_id)
            if thread is not None:
                return thread
            if self._root_run_id is not None:
                raise ValueError(f"agent not found in current session: {agent_id}")
        for metadata in self.repository.list():
            thread = self.repository.load(metadata.id).agent_state.get(agent_id)
            if thread is not None:
                return thread
        raise ValueError(f"agent not found: {agent_id}")

    def _bound_context(self) -> tuple[str, str]:
        if self._session_id is None or self._root_run_id is None:
            raise RuntimeError("agent orchestrator is not bound to an active root run")
        return self._session_id, self._root_run_id

    def _validate_plan_targets(
        self,
        plan_step_id: str | None,
        criterion_ids: tuple[str, ...],
    ) -> None:
        if plan_step_id is None:
            if criterion_ids:
                raise ValueError("criterion_ids require plan_step_id")
            return
        if self.plan_provider is None:
            raise ValueError("plan targets are unavailable")
        plan = self.plan_provider()
        step = next((item for item in plan.steps if item.id == plan_step_id), None)
        if step is None:
            raise ValueError(f"unknown plan step id: {plan_step_id}")
        valid = {item.id for item in step.acceptance_criteria}
        if not set(criterion_ids) <= valid:
            raise ValueError("criterion_ids must reference criteria on the selected step")

    def _recover_session(self, session_id: str) -> None:
        state = self.repository.load(session_id).agent_state
        for thread in state.threads:
            if thread.status not in {
                AgentStatus.QUEUED,
                AgentStatus.RUNNING,
                AgentStatus.APPROVAL_PENDING,
            }:
                continue
            if thread.config.workspace_mode is WorkspaceMode.READ_ONLY:
                self._schedule(thread)
                continue
            classifier = getattr(self.runtime_factory, "classify_recovery", None)
            status = (
                classifier(thread)
                if classifier is not None
                else (
                    AgentStatus.IMPORT_PENDING
                    if thread.commit
                    else AgentStatus.RECONCILIATION_REQUIRED
                )
            )
            kind = (
                AgentEventKind.IMPORT_PENDING
                if status is AgentStatus.IMPORT_PENDING
                else AgentEventKind.RECONCILIATION_REQUIRED
            )
            updated = thread.model_copy(update={"status": status, "updated_at": utc_now()})
            self.repository.append_agent_thread(session_id, updated)
            task = asyncio.create_task(self._emit(updated, kind))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    def _default_task_name(task: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:32]
        return value or "agent"

    @staticmethod
    def _unique_task_name(state: SessionAgentState, requested: str) -> str:
        base = re.sub(r"[^a-z0-9_-]+", "-", requested.lower()).strip("-_")[:48] or "agent"
        known = {item.task_name for item in state.threads}
        if base not in known:
            return base
        index = 2
        while f"{base}-{index}" in known:
            index += 1
        return f"{base}-{index}"

    @staticmethod
    def _idempotency_key(
        session_id: str,
        root_run_id: str,
        agent_type: str,
        task_name: str,
        task: str,
    ) -> str:
        body = "\0".join((session_id, root_run_id, agent_type, task_name, task)).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    @staticmethod
    def _json(value: object) -> str:
        if hasattr(value, "model_dump"):
            return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)  # type: ignore[union-attr]
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _merge_usage(
        previous: dict[str, object],
        current: dict[str, object],
    ) -> dict[str, object]:
        merged = dict(previous)
        for key, value in current.items():
            old = merged.get(key, 0)
            if (
                not isinstance(value, bool)
                and isinstance(value, int | float)
                and not isinstance(old, bool)
                and isinstance(old, int | float)
            ):
                merged[key] = old + value
            else:
                merged[key] = value
        return merged


__all__ = ["AgentOrchestrator", "AgentRuntimeFactory"]
