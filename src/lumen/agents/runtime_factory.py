"""Production AgentRuntime factory and isolated Git worktree execution."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import cast

from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.settings import ModelSettings

from lumen.agent_loop import PydanticAIModelDriver
from lumen.config import (
    AgentsConfig,
    ContextConfig,
    LimitsConfig,
    ModelSettingsConfig,
    PermissionsConfig,
    SandboxConfig,
)
from lumen.context import ContextEngine
from lumen.context.artifacts import ArtifactStore
from lumen.context.instructions import InstructionSource
from lumen.events import ApprovalRequest, ProgressReported, ToolCallStarted
from lumen.interactive_queue import QueueMode
from lumen.models import build_model, build_native_tools
from lumen.reasoning import ReasoningSelection, resolve_reasoning
from lumen.runtime import AgentRuntime, ApprovalHandler, CompletionPolicy, ToolApproval
from lumen.sandbox import SandboxRunner
from lumen.task_control import CONTROL_TOOL_NAMES
from lumen.tools.builtin import build_builtin_specs
from lumen.tools.capability import run_prepared_command
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency, ToolSpec
from lumen.work_products import EffectReceipt, EffectStatus, TaskWorkspace

from .orchestrator import AgentRuntimeFactory
from .types import (
    AgentConfigSnapshot,
    AgentExecutionResult,
    AgentMessage,
    AgentProfile,
    AgentStatus,
    AgentThreadState,
    AgentToolPolicy,
    WorkspaceMode,
)


class NativeAgentRuntimeFactory(AgentRuntimeFactory):
    """Create bounded child AgentRuntime instances from parent effective state."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: AgentsConfig,
        limits: LimitsConfig,
        sandbox: SandboxConfig,
        model_registry: dict[str, ModelSettingsConfig],
        active_model_name: Callable[[], str],
        parent_tools: Callable[[], Sequence[Tool[None]]],
        parent_tool_metadata: Callable[[], dict[str, dict[str, str]]],
        parent_capability_gateway: Callable[[], CapabilityGateway],
        enabled_builtins: Sequence[str],
        artifacts: ArtifactStore,
        context_config: ContextConfig,
        task_workspace: TaskWorkspace | None = None,
        parent_reasoning: Callable[[], ReasoningSelection | None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config
        self.limits = limits
        self.sandbox = sandbox
        self.model_registry = model_registry
        self.active_model_name = active_model_name
        self.parent_tools = parent_tools
        self.parent_tool_metadata = parent_tool_metadata
        self.parent_capability_gateway = parent_capability_gateway
        self.enabled_builtins = tuple(enabled_builtins)
        self.artifacts = artifacts
        self.context_config = context_config
        self.task_workspace = task_workspace
        self.parent_reasoning = parent_reasoning
        self._git_lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()
        self._approval_handler: ApprovalHandler | None = None
        self._status_handler: Callable[[str, AgentStatus], Awaitable[None]] | None = None
        self._progress_handler: Callable[[str, str, str | None], Awaitable[None]] | None = None
        self._active_runtimes: dict[str, AgentRuntime] = {}

    def bind_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    def bind_status_handler(
        self,
        handler: Callable[[str, AgentStatus], Awaitable[None]] | None,
    ) -> None:
        self._status_handler = handler

    def bind_progress_handler(
        self,
        handler: Callable[[str, str, str | None], Awaitable[None]] | None,
    ) -> None:
        """Bind the (agent_id, summary, detail) progress sink used by child runs.

        Child process events are otherwise invisible to the parent timeline;
        the orchestrator turns this stream into auditable ``agent.progress``
        records.
        """
        self._progress_handler = handler

    def snapshot(self, profile: AgentProfile, *, approval_mode: str) -> AgentConfigSnapshot:
        model_name = profile.model or self.active_model_name()
        try:
            model = self.model_registry[model_name]
        except KeyError as error:
            raise ValueError(
                f"agent profile {profile.name!r} references unknown model {model_name!r}"
            ) from error
        metadata = self.parent_tool_metadata()
        candidates = set(metadata)
        candidates -= {
            "spawn_agent",
            "send_message",
            "followup_task",
            "wait_agent",
            "interrupt_agent",
            "list_agents",
            "close_agent",
            "spawn_child",
            "wait_children",
            "cancel_child",
        }
        if profile.tools is not None:
            unknown = set(profile.tools) - candidates
            if unknown:
                raise ValueError(
                    f"agent profile {profile.name!r} requests unavailable tools: {sorted(unknown)}"
                )
            candidates &= set(profile.tools)
        if profile.workspace_mode is WorkspaceMode.READ_ONLY:
            candidates = {
                name
                for name in candidates
                if metadata.get(name, {}).get("effect") == EffectKind.OBSERVE.value
            }
        else:
            # Writable child tools must be rebound to the isolated worktree.
            # Remote toolsets remain parent-owned and are not silently copied.
            candidates = {
                name
                for name in candidates
                if name in set(self.enabled_builtins) | {"read_artifact"}
                or metadata.get(name, {}).get("origin", "").startswith("mcp:")
            }
        request_limits = [v for v in (self.config.request_count, self.limits.request_count) if v is not None]
        tool_limits = [v for v in (self.config.tool_calls, self.limits.tool_calls) if v is not None]
        inherited = (
            self.parent_reasoning()
            if self.parent_reasoning is not None and model_name == self.active_model_name() else None
        )
        reasoning = (
            resolve_reasoning(model, profile.reasoning_effort, source="agent_profile")
            if profile.reasoning_effort is not None
            else inherited or resolve_reasoning(model)
        )
        return AgentConfigSnapshot(
            model_name=model_name,
            model_id=model.id,
            reasoning_effort=profile.reasoning_effort,
            reasoning=reasoning,
            tool_names=tuple(sorted(candidates)),
            tool_policies=tuple(
                AgentToolPolicy(
                    name=name,
                    origin=metadata.get(name, {}).get("origin", "unknown"),
                    risk=metadata.get(name, {}).get("risk", "external_unknown"),
                    effect_kind=metadata.get(name, {}).get(
                        "effect", EffectKind.UNKNOWN.value
                    ),
                )
                for name in sorted(candidates)
            ),
            approval_mode=approval_mode,
            sandbox_mode=self.sandbox.mode,
            workspace_mode=profile.workspace_mode,
            cwd=str(self.workspace),
            request_limit=min(request_limits) if request_limits else None,
            tool_call_limit=min(tool_limits) if tool_limits else None,
            timeout_seconds=self.config.timeout_seconds,
            profile_revision=profile.revision,
        )

    async def execute(
        self,
        thread: AgentThreadState,
        profile: AgentProfile,
        messages: Sequence[AgentMessage],
    ) -> AgentExecutionResult:
        if thread.config.workspace_mode is WorkspaceMode.WORKTREE:
            return await self._execute_worktree(thread, profile, messages)
        return await self._execute_runtime(thread, profile, messages, self.workspace)

    async def import_changes(self, thread: AgentThreadState) -> AgentExecutionResult:
        started = time.monotonic()
        async with self._import_lock:
            result = await self._import_changes(thread)
        return result.model_copy(update={
            "usage": {**result.usage, "worktree_import_seconds": round(time.monotonic() - started, 6)},
        })

    async def _import_changes(self, thread: AgentThreadState) -> AgentExecutionResult:
        if not thread.commit:
            raise ValueError("agent has no commit to import")
        current_head = (await self._git(self.workspace, "rev-parse", "HEAD")).strip()
        child_paths = set(
            (await self._git(
                self.workspace,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                thread.commit,
            )).splitlines()
        )
        dirty_paths = (await self._dirty_paths(self.workspace))
        overlap = sorted(child_paths & dirty_paths)
        if overlap:
            return AgentExecutionResult(
                status=AgentStatus.RECONCILIATION_REQUIRED,
                output=f"import conflicts with user changes: {overlap}",
                error="child delta overlaps dirty parent paths",
                worktree=thread.worktree,
                branch=thread.branch,
                base_commit=thread.base_commit,
                commit=thread.commit,
            )
        integration = Path(self.config.worktree_root) / f"integration-{thread.ref.id}"
        if integration.exists():
            raise FileExistsError(f"integration worktree already exists: {integration}")
        integration.parent.mkdir(parents=True, exist_ok=True)
        try:
            (await self._git(self.workspace, "worktree", "add", "--detach", str(integration), current_head))
            preflight = (await self._git_result(integration, "cherry-pick", thread.commit))
            if preflight.returncode != 0:
                (await self._git_result(integration, "cherry-pick", "--abort"))
                return AgentExecutionResult(
                    status=AgentStatus.RECONCILIATION_REQUIRED,
                    output="import preflight found a merge conflict",
                    error=preflight.stderr.strip(),
                    worktree=thread.worktree,
                    branch=thread.branch,
                    base_commit=thread.base_commit,
                    commit=thread.commit,
                )
            (await self._git(integration, "diff", "--check", f"{current_head}..HEAD"))
        finally:
            (await self._git_result(self.workspace, "worktree", "remove", "--force", str(integration)))
        if (await self._git(self.workspace, "rev-parse", "HEAD")).strip() != current_head:
            return AgentExecutionResult(
                status=AgentStatus.RECONCILIATION_REQUIRED,
                output="main HEAD changed during import preflight",
                error="main HEAD changed",
                worktree=thread.worktree,
                branch=thread.branch,
                base_commit=thread.base_commit,
                commit=thread.commit,
            )
        prepared_effects: tuple[str, ...] = ()
        import_diff = (await self._git(
            self.workspace,
            "show",
            "--format=",
            "--no-ext-diff",
            "--no-renames",
            thread.commit,
        ))
        if self.task_workspace is not None:
            try:
                self.task_workspace.bind_session(thread.ref.parent_session_id)
                prepared_effects = self.task_workspace.prepare_import_effects(
                    tuple(sorted(child_paths)),
                    summary=f"prepared isolated Agent commit {thread.commit}",
                )
            except Exception as error:
                return AgentExecutionResult(
                    status=AgentStatus.RECONCILIATION_REQUIRED,
                    output="could not prepare TaskWorkspace import journal",
                    error=str(error),
                    worktree=thread.worktree,
                    branch=thread.branch,
                    base_commit=thread.base_commit,
                    commit=thread.commit,
                )
        imported = (await self._git_result(self.workspace, "cherry-pick", thread.commit))
        if imported.returncode != 0:
            (await self._git_result(self.workspace, "cherry-pick", "--abort"))
            if self.task_workspace is not None:
                self.task_workspace.finalize_import_effects(
                    prepared_effects,
                    applied=False,
                    diff=import_diff,
                )
            return AgentExecutionResult(
                status=AgentStatus.RECONCILIATION_REQUIRED,
                output="child import failed and was aborted",
                error=imported.stderr.strip(),
                worktree=thread.worktree,
                branch=thread.branch,
                base_commit=thread.base_commit,
                commit=thread.commit,
            )
        if self.task_workspace is not None:
            try:
                verified = self.task_workspace.finalize_import_effects(
                    prepared_effects,
                    applied=True,
                    diff=import_diff,
                )
            except Exception as error:
                return AgentExecutionResult(
                    status=AgentStatus.RECONCILIATION_REQUIRED,
                    output="Agent commit was imported but verification failed",
                    error=str(error),
                    worktree=thread.worktree,
                    branch=thread.branch,
                    base_commit=thread.base_commit,
                    commit=thread.commit,
                )
            if not verified:
                return AgentExecutionResult(
                    status=AgentStatus.RECONCILIATION_REQUIRED,
                    output="Agent commit was imported but verification did not pass",
                    error="TaskWorkspace import verification failed",
                    worktree=thread.worktree,
                    branch=thread.branch,
                    base_commit=thread.base_commit,
                    commit=thread.commit,
                )
        await self.close(thread)
        return AgentExecutionResult(
            status=AgentStatus.IMPORTED,
            output=f"imported {thread.commit}",
            commit=thread.commit,
        )

    async def reject_changes(self, thread: AgentThreadState) -> None:
        await self.close(thread)

    def queue_message(self, thread: AgentThreadState, message: str) -> bool:
        runtime = self._active_runtimes.get(thread.ref.id)
        if runtime is None:
            return False
        runtime.interactive_queue.enqueue(
            message,
            f"<parent-agent-message>\n{message}\n</parent-agent-message>",
            QueueMode.STEER,
        )
        return True

    async def classify_recovery(self, thread: AgentThreadState) -> AgentStatus:
        """Conservatively classify a persisted writable Agent after restart."""

        if thread.config.workspace_mode is WorkspaceMode.READ_ONLY:
            return AgentStatus.QUEUED
        if thread.commit:
            commit = (await self._git_result(
                self.workspace,
                "cat-file",
                "-e",
                f"{thread.commit}^{{commit}}",
            ))
            if commit.returncode == 0:
                return AgentStatus.IMPORT_PENDING
        return AgentStatus.RECONCILIATION_REQUIRED

    async def close(self, thread: AgentThreadState) -> None:
        if thread.worktree:
            (await self._git_result(self.workspace, "worktree", "remove", "--force", thread.worktree))
        if thread.branch:
            (await self._git_result(self.workspace, "branch", "-D", thread.branch))

    async def _execute_worktree(
        self,
        thread: AgentThreadState,
        profile: AgentProfile,
        messages: Sequence[AgentMessage],
    ) -> AgentExecutionResult:
        started = time.monotonic()
        root = (await self._git(self.workspace, "rev-parse", "--show-toplevel")).strip()
        if Path(root).resolve() != self.workspace:  # noqa: ASYNC240 - synchronous Git boundary
            raise RuntimeError("writable agents require the workspace Git root")
        base = thread.base_commit or (await self._git(self.workspace, "rev-parse", "HEAD")).strip()
        branch = thread.branch or (
            f"lumen/agent/{thread.ref.parent_session_id[:8]}/"
            f"{thread.ref.id.removeprefix('agent-')}"
        )
        worktree = (
            Path(thread.worktree)
            if thread.worktree
            else Path(self.config.worktree_root) / thread.ref.id
        )
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            (await self._git(self.workspace, "worktree", "add", "-b", branch, str(worktree), base))
        prepared_seconds = time.monotonic() - started
        execution = await self._execute_runtime(thread, profile, messages, worktree)
        finalizing = time.monotonic()
        execution = execution.model_copy(update={
            "usage": {**execution.usage, "worktree_prepare_seconds": round(prepared_seconds, 6)},
        })
        status = (await self._git(worktree, "status", "--porcelain"))
        if not status.strip():
            return execution.model_copy(
                update={
                    "status": AgentStatus.COMPLETED,
                    "worktree": str(worktree),
                    "branch": branch,
                    "base_commit": base,
                    "usage": {**execution.usage,
                              "worktree_finalize_seconds": round(time.monotonic() - finalizing, 6)},
                }
            )
        (await self._git(worktree, "diff", "--check"))
        (await self._git(worktree, "add", "-A"))
        (await self._git(
            worktree,
            "-c",
            "user.name=Lumen Agent",
            "-c",
            "user.email=lumen-agent@local",
            "commit",
            "-m",
            f"lumen agent {thread.task_name}: {thread.task[:72]}",
        ))
        commit = (await self._git(worktree, "rev-parse", "HEAD")).strip()
        diff = (await self._git(worktree, "show", "--stat", "--oneline", "--no-renames", commit))
        return execution.model_copy(
            update={
                "status": AgentStatus.IMPORT_PENDING,
                "worktree": str(worktree),
                "branch": branch,
                "base_commit": base,
                "commit": commit,
                "diff": diff,
                "usage": {**execution.usage,
                          "worktree_finalize_seconds": round(time.monotonic() - finalizing, 6)},
            }
        )

    async def _execute_runtime(
        self,
        thread: AgentThreadState,
        profile: AgentProfile,
        messages: Sequence[AgentMessage],
        cwd: Path,
    ) -> AgentExecutionResult:
        model_config = self.model_registry[thread.config.model_name]
        if model_config.id != thread.config.model_id:
            raise ValueError("Agent model configuration changed; restore its original model ID to continue")
        tools, metadata = self._tools_for(thread, cwd)
        effective_model_settings = dict(model_config.settings)
        reasoning = thread.config.reasoning or resolve_reasoning(
            model_config, thread.config.reasoning_effort, source="agent_profile",
        )
        effect_receipts: list[EffectReceipt] = []

        def record_effect(
            *,
            tool_name: str,
            effect_kind: EffectKind,
            success: bool,
            summary: str,
            **_extra: object,
        ) -> None:
            status = (
                EffectStatus.RECONCILIATION_REQUIRED
                if success and effect_kind is EffectKind.UNKNOWN
                else EffectStatus.VERIFIED
                if success
                else EffectStatus.FAILED
            )
            effect_receipts.append(
                EffectReceipt(
                    id=f"effect:agent:{thread.ref.id}:{len(effect_receipts) + 1}",
                    effect_kind=effect_kind,
                    operation=tool_name,
                    status=status,
                    summary=summary,
                    error=None if success else summary,
                )
            )

        child_model = build_model(model_config)
        replacement_specs: list[tuple[ToolSpec, str]] = []
        for tool in tools:
            document = metadata.get(tool.name, {})
            try:
                risk = Risk(document.get("risk", Risk.EXTERNAL_UNKNOWN.value))
            except ValueError:
                risk = Risk.EXTERNAL_UNKNOWN
            try:
                effect = EffectKind(document.get("effect", EffectKind.UNKNOWN.value))
            except ValueError:
                effect = EffectKind.UNKNOWN
            replacement_specs.append(
                (
                    ToolSpec(
                        tool.function,
                        name=tool.name,
                        description=tool.description,
                        timeout=tool.timeout,
                        risk=risk,
                        effect_kind=effect,
                        concurrency=(
                            None
                            if tool.sequential
                            else lambda _arguments: ToolConcurrency.PARALLEL_SAFE
                        ),
                    ),
                    document.get("origin", "child-runtime"),
                )
            )
        capability_gateway = self.parent_capability_gateway().narrow(
            thread.config.tool_names,
            replacements=replacement_specs,
            effect_recorder=record_effect,
        )
        child_instructions = (
            f"{profile.instructions}\n\n"
            "你是一层深度的子 Agent。不要创建或协调其他 Agent。"
            "向父 Agent 返回简洁且有证据支持的结果。"
        )
        runtime = AgentRuntime(
            model=child_model,
            tools=tools,
            toolsets=(),
            instructions=child_instructions,
            prompt_mode="replace",
            prompt_version=profile.revision,
            prompt_sources=(
                InstructionSource(
                    origin=profile.file_path or f"builtin:agent-profile:{profile.name}",
                    role="system",
                    text=child_instructions,
                    revision=profile.revision,
                ),
            ),
            limits=self.limits.model_copy(
                update={
                    "request_count": thread.config.request_limit,
                    "tool_calls": thread.config.tool_call_limit,
                    "parallel_tool_calls": "parallel_safe",
                }
            ),
            tool_metadata=metadata,
            model_settings=cast(ModelSettings, effective_model_settings),
            native_tools=build_native_tools(model_config),
            effect_recorder=record_effect,
            context_engine=ContextEngine(
                config=self.context_config,
                model=child_model,
                model_id=model_config.id,
                model_config=model_config,
                artifact_root=str(self.artifacts.root),
            ),
            capability_gateway=capability_gateway,
            model_driver=PydanticAIModelDriver(child_model),
            lumen_model_route=model_config.id,
        )
        self._active_runtimes[thread.ref.id] = runtime
        history = self._load_history(thread.history_ref)

        async def emit(event: object) -> None:
            # Child process events flow to the parent timeline as bounded
            # progress summaries instead of a verbatim event copy: the child's
            # own transcript stays in its history artifact, and only
            # significant steps (non-control tool calls, progress reports)
            # surface to the parent.
            handler = self._progress_handler
            if handler is None:
                return
            summary: str | None = None
            detail: str | None = None
            if isinstance(event, ToolCallStarted):
                if event.origin == "control" or event.name in CONTROL_TOOL_NAMES:
                    return
                summary = event.name
                target = event.args.get("path") or event.args.get("query") or event.args.get("url")
                detail = str(target) if target is not None else None
            elif isinstance(event, ProgressReported):
                summary = event.summary
                detail = event.next_action
            else:
                return
            await handler(thread.ref.id, summary, detail)

        async def approve(request: ApprovalRequest) -> ToolApproval:
            risk = request.risk
            origin = request.origin
            if risk == "read" or (
                thread.config.workspace_mode is WorkspaceMode.WORKTREE and origin == "builtin"
            ):
                return ToolApproval(True, "approved by inherited child isolation policy")
            if self._approval_handler is not None:
                if self._status_handler is not None:
                    await self._status_handler(thread.ref.id, AgentStatus.APPROVAL_PENDING)
                try:
                    return await self._approval_handler(
                        ApprovalRequest(
                            call_id=request.call_id,
                            name=request.name,
                            args=request.args,
                            origin=f"agent:{thread.ref.path}:{request.origin}",
                            risk=request.risk,
                        )
                    )
                finally:
                    if self._status_handler is not None:
                        await self._status_handler(thread.ref.id, AgentStatus.RUNNING)
            return ToolApproval(False, "child external action requires parent coordination")

        prompt = thread.task
        queued = [item.content for item in messages if item.content]
        if queued:
            prompt += "\n\nParent messages:\n" + "\n".join(f"- {item}" for item in queued[-8:])
        try:
            runtime.configure_reasoning(reasoning)
            async with runtime:
                outcome = await runtime.run(
                    prompt,
                    history,
                    emit,  # type: ignore[arg-type]
                    approve,  # type: ignore[arg-type]
                    session_id=f"{thread.ref.parent_session_id}:{thread.ref.id}",
                    completion_policy=CompletionPolicy(
                        require_post_mutation_verification=False,
                        max_retries=1,
                    ),
                )
        finally:
            self._active_runtimes.pop(thread.ref.id, None)
        transcript = ModelMessagesTypeAdapter.dump_json(
            [*history, *outcome.new_messages]
        ).decode("utf-8")
        status = {
            "completed": AgentStatus.COMPLETED,
            "waiting_for_user": AgentStatus.WAITING,
        }.get(outcome.status, AgentStatus.FAILED)
        if status is AgentStatus.COMPLETED and any(
            item.status is EffectStatus.RECONCILIATION_REQUIRED
            for item in effect_receipts
        ):
            status = AgentStatus.RECONCILIATION_REQUIRED
        return AgentExecutionResult(
            status=status,
            output=outcome.output,
            transcript=transcript,
            usage=outcome.usage,
            error=None if status in {AgentStatus.COMPLETED, AgentStatus.WAITING} else outcome.output,
            effect_receipts=tuple(effect_receipts),
        )

    def _tools_for(
        self,
        thread: AgentThreadState,
        cwd: Path,
    ) -> tuple[list[Tool[None]], dict[str, dict[str, str]]]:
        allowed = set(thread.config.tool_names)
        parent_metadata = {
            policy.name: {
                "origin": policy.origin,
                "risk": policy.risk,
                "effect": policy.effect_kind,
            }
            for policy in thread.config.tool_policies
        }
        # v8 records written by early builds did not include the policy tuple;
        # names remain safely bounded, so use current metadata only to load them.
        if not parent_metadata:
            current = self.parent_tool_metadata()
            parent_metadata = {
                name: current[name] for name in allowed if name in current
            }
        if thread.config.workspace_mode is WorkspaceMode.READ_ONLY:
            tools = [tool for tool in self.parent_tools() if tool.name in allowed]
            return tools, {name: parent_metadata[name] for name in allowed if name in parent_metadata}

        registry = ToolRegistry(cwd)
        child_specs = {
            str(spec.name): spec
            for spec in build_builtin_specs(
                cwd,
                max_timeout=self.limits.tool_timeout_seconds,
                sandbox_config=self.sandbox,
            )
        }
        for name in self.enabled_builtins:
            if name in allowed and name in child_specs:
                registry.add(child_specs[name], origin="builtin")
        tools = registry.build_local_tools(
            PermissionPolicy(PermissionsConfig(always_allow=sorted(registry.entries))),
            default_timeout=self.limits.tool_timeout_seconds,
            parallel_mode="parallel_safe",
        )
        metadata = {
            name: {
                "origin": entry.origin,
                "risk": entry.spec.risk.value,
                "effect": entry.spec.effect.value,
            }
            for name, entry in registry.entries.items()
        }
        # Artifact reading and web access are workspace-independent, so preserve
        # the parent's already-configured tool implementations when selected.
        portable_tools = {"read_artifact", "web_fetch", "web_search"}
        tools.extend(
            tool for tool in self.parent_tools() if tool.name in portable_tools and tool.name in allowed
        )
        for name in portable_tools & allowed:
            if name in parent_metadata:
                metadata[name] = parent_metadata[name]
        return tools, metadata

    def _load_history(self, ref: str | None) -> list[ModelMessage]:
        if ref is None:
            return []
        body = self.artifacts.read(ref)
        if body is None:
            return []
        return list(ModelMessagesTypeAdapter.validate_json(body))

    async def _dirty_paths(self, cwd: Path) -> set[str]:
        output = (await self._git(cwd, "status", "--porcelain"))
        paths: set[str] = set()
        for line in output.splitlines():
            value = line[3:] if len(line) >= 4 else ""
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            if value:
                paths.add(value)
        return paths

    async def _git_result(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        # Host-owned lifecycle Git commands are serialized. Child model tools
        # retain their separately narrowed sandbox and approval policy.
        sandbox = SandboxRunner(cwd, SandboxConfig(mode="disabled"))
        argv = ["git", *args]
        async with self._git_lock:
            result = await run_prepared_command(
                sandbox.prepare(argv, cwd=cwd), argv=argv, resolved_cwd=cwd, cwd=str(cwd),
                timeout_seconds=120, sandbox_config=sandbox.config,
            )
        if result["timed_out"]:
            raise TimeoutError("Agent Git operation timed out")
        if result["stdout_truncated"] or result["stderr_truncated"]:
            raise RuntimeError("Agent Git output exceeds safe capture; refusing partial evidence")
        return subprocess.CompletedProcess(argv, int(result["exit_code"]), result["stdout"], result["stderr"])

    async def _git(self, cwd: Path, *args: str) -> str:
        result = (await self._git_result(cwd, *args))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout

    async def parent_dirty_hash(self, workspace: Path) -> str:
        result = (await self._git(workspace, "status", "--porcelain"))
        return "sha256:" + hashlib.sha256(result.encode()).hexdigest()


__all__ = ["NativeAgentRuntimeFactory"]
