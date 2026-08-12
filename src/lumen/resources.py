from __future__ import annotations

import subprocess
import sys
from contextlib import AsyncExitStack
from functools import partial
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from lumen.agents.compat import LegacyChildRunAdapter
from lumen.agents.orchestrator import AgentOrchestrator
from lumen.agents.profiles import AgentProfileLoader
from lumen.agents.runtime_factory import NativeAgentRuntimeFactory
from lumen.branding import FRAMEWORK_NAME
from lumen.completion import CompletionGate
from lumen.config import AppConfig, ModelSettingsConfig
from lumen.context import ArtifactStore, ArtifactStoreError, ContextEngine
from lumen.context.memory import (
    ExtractionProvenance,
    MemoryManager,
    RawFact,
    SessionExtractionSource,
    SQLiteMemoryRepository,
    SQLiteMemoryWorkQueue,
)
from lumen.context.memory.redaction import redact_secrets
from lumen.context.rendering import render_mcp_prompt
from lumen.context.session_manager import SessionContextManager
from lumen.events import ToolCallFinished, ToolCallStarted
from lumen.hooks import HookBus
from lumen.live.factory import build_live_router
from lumen.live.manager import LiveSessionManager
from lumen.mcp_resources import McpContentRegistry
from lumen.mcp_tools import McpToolsetBundle, build_mcp_toolset
from lumen.models import build_model
from lumen.plan import EvidenceReceipt, PlanState
from lumen.runtime import CONTROL_INSTRUCTIONS, AgentRuntime
from lumen.sandbox import SandboxRunner
from lumen.sessions import SessionRepository
from lumen.skills import (
    Skill,
    SkillLoader,
    SkillWorkingSet,
    expand_skill_for_message,
    format_skills_for_prompt,
)
from lumen.tools.builtin import build_builtin_specs
from lumen.tools.gateway import CapabilityDescriptor, CapabilityGateway
from lumen.tools.registry import DuplicateToolError, PermissionPolicy, ToolRegistry, load_plugin_specs
from lumen.tools.spec import EffectKind, Risk, ToolSpec
from lumen.tools.workspace import WorkspaceViolation
from lumen.trust import canonical_project_identity
from lumen.work_products import TaskWorkspace

BASE_INSTRUCTIONS = f"""You are {FRAMEWORK_NAME}, an open-source agent framework.
You run in the user's workspace.
Your identity is {FRAMEWORK_NAME}; the configured language model is an interchangeable inference provider,
not your product identity. Do not claim to be Claude, ChatGPT, DeepSeek, Qwen, GLM, or another model vendor.
When asked what you are, distinguish the {FRAMEWORK_NAME} framework from the active model
shown by the runtime.
The {FRAMEWORK_NAME} codebase is implemented primarily in Python unless workspace evidence says otherwise.
Assess each request and use available tools only when they improve correctness or are needed to act.
Use tool results as evidence, never invent a result, and recover gracefully when a tool fails or is denied.
When the user requests a generated report, export, document, or other deliverable without an explicit path,
write it under outputs/ with a descriptive filename. Keep source-code changes at their actual project paths.
For multi-turn changes to an existing deliverable or structured file, open it as a work product,
inspect the requested target, and use a constrained change so unrelated content is preserved and verified.
Native Agent Threads are available when enabled. If the user, project instructions, or an active Skill
explicitly requires subagents or parallel agents, delegate independent tasks, wait for every requested
Agent, and synthesize their evidence before completing. Otherwise delegate adaptively only when parallel
work materially improves speed, context isolation, or verification. Avoid overlapping writable tasks.
Keep user-facing explanations concise. Do not reveal private chain-of-thought;
provide only brief useful rationale.
When the task is complete, answer the user directly.
"""

# Names the runtime reserves for control tools; the resource manager refuses to
# register any builtin/plugin/MCP tool that collides so a misconfigured plugin
# cannot shadow the planning and progress channel.
CONTROL_TOOL_NAMES = frozenset(
    {"set_plan", "update_step", "link_evidence", "report_progress", "request_clarification"}
)
CHILD_TOOL_NAMES = frozenset({"spawn_child", "wait_children", "cancel_child"})
AGENT_TOOL_NAMES = frozenset(
    {
        "spawn_agent",
        "send_message",
        "followup_task",
        "wait_agent",
        "interrupt_agent",
        "list_agents",
        "close_agent",
    }
)
RESERVED_TOOL_NAMES = CONTROL_TOOL_NAMES | CHILD_TOOL_NAMES | AGENT_TOOL_NAMES


class ResourceStartupError(RuntimeError):
    """Raised when configured runtime resources cannot be initialized."""


async def _guarded_mcp_client_exit(client: MCPToolset[None], *_exc_info: object) -> None:
    """Close a persisted MCP client, tolerating a reconnect-drained count.

    The runtime reconnect path (``ResilientMcpToolset``) exits and re-enters
    the toolset to rebuild broken sessions. When the server stays dead the
    re-enter never happens, so this final exit would raise ``ValueError``
    ("more exits than enters") and mask the session teardown — swallow just
    that case.
    """

    try:
        await client.__aexit__(None, None, None)
    except ValueError:
        pass


class ResourceManager:
    def __init__(self, config: AppConfig, *, workspace: str | Path) -> None:
        self.config = config
        self.workspace = Path(workspace).expanduser().resolve()
        self.warnings: list[str] = []
        self.hooks = HookBus.from_config(
            config.hooks,
            workspace=self.workspace,
            search_path=config.config_path.parent,
        )
        # Skill discovery runs before _load_instructions because the latter
        # injects the skill catalog into the system prompt. Skills are
        # disabled by config when the user sets ``agent.skills_enabled: false``.
        self.skills: list[Skill] = []
        if config.agent.skills_enabled:
            loader = SkillLoader(
                self.workspace,
                include_project=config.project_trusted,
                include_builtin=config.agent.builtin_skills_enabled,
            )
            self.skills = loader.discover()
            self.warnings.extend(loader.warnings)
        profile_loader = AgentProfileLoader(
            self.workspace,
            include_project=config.project_trusted,
        )
        self.agent_profiles = profile_loader.discover()
        self.warnings.extend(profile_loader.warnings)
        # Retained as a compatibility surface for callers that inspect the old
        # object; it is no longer the authority for active Skill bodies.
        self.skill_working_set = SkillWorkingSet()
        self.instructions = self._load_instructions()
        self.sandbox_runner = SandboxRunner(self.workspace, config.sandbox)
        self.policy = PermissionPolicy(config.permissions)
        self.registry = ToolRegistry(self.workspace)
        # TaskWorkspace and builtin write/edit tools share the same artifact
        # store and repository. They are constructed before tool registration
        # so legacy mutations can cross the same seam without changing their
        # public interface.
        self._artifact_store = ArtifactStore(Path.home() / ".lumen" / "artifacts")
        self.session_repository = SessionRepository(config.sessions.directory)
        self.task_workspace = TaskWorkspace(
            self.workspace,
            self._artifact_store,
            self.session_repository,
            enabled=config.work_products.enabled,
            auto_attach=config.work_products.auto_attach,
            strict=config.work_products.strict,
            max_context_items=config.work_products.max_context_items,
        )
        if config.work_products.enabled:
            for function, risk, effect in (
                (self.task_workspace.open_work_product, Risk.READ, EffectKind.OBSERVE),
                (self.task_workspace.inspect_work_product, Risk.READ, EffectKind.OBSERVE),
                (self.task_workspace.change_work_product, Risk.WRITE, EffectKind.MUTATION),
                (self.task_workspace.restore_work_product, Risk.WRITE, EffectKind.MUTATION),
            ):
                self.registry.add(
                    ToolSpec(function, risk=risk, effect_kind=effect),
                    origin="builtin:work_products",
                )
        if any(not skill.disable_model_invocation for skill in self.skills):
            self.registry.add(
                ToolSpec(
                    self._load_model_skill,
                    name="load_skill",
                    description="Load the full instructions for a discovered skill by name.",
                    risk=Risk.READ,
                ),
                origin="builtin:skills",
            )
            self.registry.add(
                ToolSpec(
                    self._read_model_skill_resource,
                    name="read_skill_resource",
                    description=(
                        "Read a UTF-8 text file relative to a discovered skill directory. "
                        "Use this for files referenced by a loaded skill; absolute paths and "
                        "paths that escape the skill directory are rejected."
                    ),
                    risk=Risk.READ,
                ),
                origin="builtin:skills",
            )
        if any(skill.scripts for skill in self.skills):
            self.registry.add(
                ToolSpec(
                    self._run_skill_script,
                    name="run_skill_script",
                    description=(
                        "Run a script explicitly declared by a discovered skill. "
                        "The script executes in its skill directory with a sanitized environment."
                    ),
                    risk=Risk.EXECUTE,
                    timeout=config.agent.limits.skill_script_timeout_seconds,
                ),
                origin="builtin:skills",
            )
        builtins = {
            spec.name: spec
            for spec in build_builtin_specs(
                self.workspace,
                max_timeout=config.agent.limits.tool_timeout_seconds,
                sandbox_config=config.sandbox,
                task_workspace=self.task_workspace,
            )
        }
        for name in config.tools.builtins:
            spec = builtins[name]
            self.registry.add(spec, origin="builtin")
        for plugin in config.tools.plugins:
            self.registry.add_many(
                load_plugin_specs(plugin, search_path=plugin.source_dir or config.config_path.parent),
                origin=f"plugin:{plugin.module}",
            )
        reserved_conflicts = set(self.registry.entries) & RESERVED_TOOL_NAMES
        if reserved_conflicts:
            raise DuplicateToolError(
                f"tools conflict with reserved runtime names: {sorted(reserved_conflicts)}"
            )
        # Shared content-addressed store: the context engine spills bulky tool
        # outputs here (same root, so refs resolve identically), and the
        # ``read_artifact`` tool reads them back on demand.
        self.registry.add(
            ToolSpec(
                self._read_artifact,
                name="read_artifact",
                description=(
                    "Read the full body of a spilled tool output by its artifact ref "
                    "(sha256:<hex>). Use this when a tool-receipt in the conversation history "
                    "shows 'artifact: sha256:...' and you need detail beyond its head/tail "
                    "excerpt. Large bodies can be paged with start/max_chars."
                ),
                risk=Risk.READ,
            ),
            origin="builtin:artifacts",
        )
        self.local_tools = self.registry.build_local_tools(
            self.policy,
            default_timeout=config.agent.limits.tool_timeout_seconds,
            parallel_mode=config.agent.limits.parallel_tool_calls,
        )
        # Initialised before the bundles so the reconnect status sink has a
        # mapping to write into once servers start flapping at runtime.
        self.mcp_status: dict[str, str] = {name: "connecting" for name in config.mcp_servers}
        self.mcp_bundles: list[McpToolsetBundle] = [
            build_mcp_toolset(
                name,
                server,
                self.policy,
                cwd=self.workspace,
                timeout=config.agent.limits.tool_timeout_seconds,
                credential_root=config.sessions.directory,
                status_sink=self._set_mcp_status,
                parallel_mode=config.agent.limits.parallel_tool_calls,
            )
            for name, server in config.mcp_servers.items()
        ]
        self.mcp_content = McpContentRegistry()
        self.session_context = SessionContextManager(
            repository=self.session_repository,
            artifacts=self._artifact_store,
            skill_loader=self._find_skill,
            resource_loader=self.mcp_content.fetch_resource,
        )
        self._active_session_id: str | None = None
        self.project_id = canonical_project_identity(self.workspace)
        self.memory_manager = self._build_memory_manager()
        self.tool_metadata: dict[str, dict[str, str]] = {
            name: {
                "origin": entry.origin,
                "risk": entry.spec.risk.value,
                "effect": entry.spec.effect.value,
            }
            for name, entry in self.registry.entries.items()
            if name not in self.policy.always_deny
        }
        # Control tools are always visible, marked so the TUI can render them
        # distinctly, and excluded from the optional read-only class.
        for control_name in CONTROL_TOOL_NAMES:
            self.tool_metadata[control_name] = {
                "origin": "control",
                "risk": "read",
                "effect": EffectKind.OBSERVE.value,
                "control": "true",
            }
        if config.agents.enabled:
            for agent_tool in AGENT_TOOL_NAMES:
                self.tool_metadata[agent_tool] = {
                    "origin": "control:agents",
                    "risk": Risk.READ.value,
                    "effect": (
                        EffectKind.EXECUTION.value
                        if agent_tool in {"spawn_agent", "followup_task", "interrupt_agent"}
                        else EffectKind.OBSERVE.value
                    ),
                    "control": "true",
                }
        if config.delegation.enabled:
            for child_tool in CHILD_TOOL_NAMES:
                self.tool_metadata[child_tool] = {
                    "origin": "control:children",
                    "risk": Risk.EXECUTE.value if child_tool == "spawn_child" else Risk.READ.value,
                    "effect": (
                        EffectKind.EXECUTION.value
                        if child_tool == "spawn_child"
                        else EffectKind.OBSERVE.value
                    ),
                    "control": "true",
                }
        self.runtime: AgentRuntime | None = None
        # Model registry derived from agent.model (legacy) or agent.models.
        self.model_registry: dict[str, ModelSettingsConfig] = config.agent.model_registry()
        self._active_model_name: str = config.agent.default_model_name()
        self.agent_runtime_factory = NativeAgentRuntimeFactory(
            workspace=self.workspace,
            config=config.agents,
            limits=config.agent.limits,
            sandbox=config.sandbox,
            model_registry=self.model_registry,
            active_model_name=lambda: self._active_model_name,
            parent_tools=lambda: tuple(self.local_tools),
            parent_tool_metadata=lambda: dict(self.tool_metadata),
            parent_toolsets=lambda: tuple(self._active_toolsets),
            enabled_builtins=config.tools.builtins,
            artifacts=self._artifact_store,
            task_workspace=self.task_workspace,
        )
        self.agent_orchestrator = AgentOrchestrator(
            workspace=self.workspace,
            config=config.agents,
            profiles=self.agent_profiles,
            repository=self.session_repository,
            artifacts=self._artifact_store,
            runtime_factory=self.agent_runtime_factory,
            plan_provider=lambda: self.runtime.controller.snapshot() if self.runtime else PlanState(),
            evidence_sink=self._record_agent_evidence,
        )
        self.child_run_manager: LegacyChildRunAdapter | None = (
            LegacyChildRunAdapter(self.agent_orchestrator) if config.agents.enabled else None
        )
        live_registry = ToolRegistry(self.workspace)
        for entry in self.registry.entries.values():
            live_registry.add(entry.spec, origin=entry.origin)
        if config.agents.enabled:
            for function, name, risk, effect in (
                (
                    self.agent_orchestrator.spawn_agent,
                    "spawn_agent",
                    Risk.READ,
                    EffectKind.EXECUTION,
                ),
                (
                    self.agent_orchestrator.send_message,
                    "send_message",
                    Risk.READ,
                    EffectKind.OBSERVE,
                ),
                (
                    self.agent_orchestrator.followup_task,
                    "followup_task",
                    Risk.READ,
                    EffectKind.EXECUTION,
                ),
                (
                    self.agent_orchestrator.wait_agent,
                    "wait_agent",
                    Risk.READ,
                    EffectKind.OBSERVE,
                ),
                (
                    self.agent_orchestrator.interrupt_agent,
                    "interrupt_agent",
                    Risk.READ,
                    EffectKind.EXECUTION,
                ),
                (
                    self.agent_orchestrator.list_agents,
                    "list_agents",
                    Risk.READ,
                    EffectKind.OBSERVE,
                ),
                (
                    self.agent_orchestrator.close_agent,
                    "close_agent",
                    Risk.READ,
                    EffectKind.OBSERVE,
                ),
            ):
                live_registry.add(
                    ToolSpec(function, name=name, risk=risk, effect_kind=effect),
                    origin="control:agents",
                )
        self.capability_gateway = CapabilityGateway(
            live_registry,
            self.policy,
            default_timeout=config.agent.limits.tool_timeout_seconds,
            effect_recorder=self.task_workspace.record_tool_effect,
        )
        self.completion_gate = CompletionGate(self.completion_issues)
        self.live_manager: LiveSessionManager | None = None
        if config.live.enabled:
            try:
                live_router = build_live_router(config.live)
            except (TypeError, ValueError) as error:
                raise ResourceStartupError(f"Live configuration is invalid: {error}") from error
            self.live_manager = LiveSessionManager(
                config=config.live,
                router=live_router,
                repository=self.session_repository,
                capability_gateway=self.capability_gateway,
                completion_gate=self.completion_gate,
                plan_provider=lambda session_id: self.session_repository.load(session_id).plan,
                context_documents=self.live_context_documents,
                bind_session=self.bind_live_session,
            )
        self._active_toolsets: list[AbstractToolset[None]] = []
        self._remote_tool_schema_documents: list[dict[str, Any]] = []
        # MCP clients live on the outer stack so model switches can rebuild the
        # runtime without reconnecting remote services. The runtime agent lives
        # on its own inner stack so it can be torn down in isolation.
        self._stack: AsyncExitStack | None = None
        self._runtime_stack: AsyncExitStack | None = None

    # -- model registry ----------------------------------------------------

    def _set_mcp_status(self, name: str, status: str) -> None:
        """Record a server's runtime health, reported by the resilient toolset.

        Called from the MCP reconnect path so ``/mcp`` reflects a server that
        dropped and re-established (or failed to re-establish) its connection
        after startup.
        """

        self.mcp_status[name] = status

    def active_model_name(self) -> str:
        """Return the logical name of the currently active model."""

        return self._active_model_name

    def set_startup_model(self, name: str) -> None:
        """Pre-open model selection for the CLI ``--model`` flag.

        Validates ``name`` against the registry and stores it as the active
        model. Must be called before ``open()``; once the runtime is open use
        ``select_model`` instead so the running Agent is rebuilt.
        """

        if name not in self.model_registry:
            raise KeyError(f"unknown model {name!r}; configured: {self.available_models()}")
        if self._stack is not None:
            raise RuntimeError(
                "set_startup_model must be called before open(); use select_model() to switch later"
            )
        self._active_model_name = name

    def active_model_config(self) -> ModelSettingsConfig:
        """Return the ``ModelSettingsConfig`` for the active model."""

        return self.model_registry[self._active_model_name]

    def _build_memory_manager(self) -> MemoryManager:
        """Build the durable-memory manager on the SQLite authority (plan §11.2).

        ``memory.use`` defaults to True (explicit recall on), while automatic
        learning is opt-in. The SQLite file lives under ``~/.lumen/state/``;
        non-user records carry a canonical project id so one authority can be
        shared across sessions and worktrees without cross-project recall.
        """

        state_dir = Path.home() / ".lumen" / "state"
        store_path = state_dir / "memory.sqlite3"
        repository = SQLiteMemoryRepository(store_path)
        work_queue = SQLiteMemoryWorkQueue(state_dir / "memory-work.sqlite3")
        return MemoryManager(
            repository,
            project_id=self.project_id,
            use=self.config.memory.use,
            learn=self.config.memory.learn,
            source_loader=self._load_memory_source,
            work_queue=work_queue,
            min_session_turns=self.config.memory.min_session_turns,
            max_attempts=self.config.memory.max_attempts,
            retry_base_seconds=self.config.memory.retry_base_seconds,
            idle_seconds=self.config.memory.idle_seconds,
            projection_dir=Path.home() / ".lumen" / "projects" / self.project_id / "memory",
        )

    def _load_memory_source(self, session_id: str) -> SessionExtractionSource | None:
        """Build a provenance-labelled extraction view from an append-only session."""

        try:
            data = self.session_repository.load(session_id)
        except (OSError, ValueError):
            return None
        lines: list[str] = []
        user_events: set[str] = set()
        external: set[str] = set()
        verified: set[str] = set()
        for turn_index, turn in enumerate(data.turns, 1):
            user_event = f"turn:{turn_index}:user"
            user_events.add(user_event)
            lines.append(f"[{user_event} source=user] {redact_secrets(turn.user_input)}")
            starts: dict[str, ToolCallStarted] = {}
            for timeline_record in turn.timeline_events:
                try:
                    event = timeline_record.to_event()
                except ValueError:
                    continue
                if isinstance(event, ToolCallStarted):
                    starts[event.call_id] = event
                elif isinstance(event, ToolCallFinished):
                    event_id = f"turn:{turn_index}:tool:{event.call_id}"
                    started = starts.get(event.call_id)
                    origin = started.origin if started is not None else "unknown"
                    risk = started.risk if started is not None else "external"
                    lines.append(
                        f"[{event_id} source={origin} status={'error' if event.is_error else 'ok'}] "
                        f"{redact_secrets(event.result)}"
                    )
                    if origin.startswith("mcp:") or risk.startswith("external"):
                        external.add(event_id)
                    elif origin in {"builtin", "control"}:
                        verified.add(event_id)
            for message_index, message in enumerate(turn.messages, 1):
                if not isinstance(message, ModelResponse):
                    continue
                text = "\n".join(part.content for part in message.parts if isinstance(part, TextPart))
                if text:
                    event_id = f"turn:{turn_index}:assistant:{message_index}"
                    lines.append(f"[{event_id} source=assistant] {redact_secrets(text)}")
        return SessionExtractionSource(
            session_id=session_id,
            transcript_text="\n".join(lines),
            turn_count=len(data.turns),
            has_stable_result=any(turn.status == "completed" for turn in data.turns),
            provenance=ExtractionProvenance(
                user_event_ids=frozenset(user_events),
                external_event_ids=frozenset(external),
                locally_verified_event_ids=frozenset(verified),
            ),
        )

    @staticmethod
    def _memory_extractor(model: Model | str) -> Any:
        """Create a no-tool extraction agent whose output must cite event ids."""

        agent: Agent[None, list[RawFact]] = Agent(
            model,
            output_type=list[RawFact],
            instructions=(
                "Extract only stable, reusable user preferences, workflows, project facts, warnings, "
                "and references. Every fact must cite one or more exact event ids from the transcript. "
                "Do not copy credentials, personal data, temporary task state, code bodies, or facts "
                "supported only by untrusted external tool output. Return an empty list when unsure."
            ),
        )

        async def extract(source: SessionExtractionSource) -> list[RawFact]:
            result = await agent.run(source.transcript_text)
            return result.output

        return extract

    def available_models(self) -> list[str]:
        """Return the sorted logical names of all configured models."""

        return sorted(self.model_registry)

    def load_skill_by_name(self, name: str) -> Skill | None:
        """Controlled seam for loading a skill's body by name.

        Returns the discovered skill matching ``name`` (re-reading its body is
        unnecessary — the body is parsed once at discovery and held on the
        ``Skill``). Returns ``None`` for any name that was not discovered and
        validated, so neither the ``/skill:`` command nor a future tool can
        use this to read an arbitrary file: only paths that survived discovery
        (confined to the project/user skill roots) are reachable.

        This is the single entry point for skill loading — prefer it over
        reaching into ``self.skills`` directly so the confinement guarantee
        holds in one place.
        """

        skill = next((candidate for candidate in self.skills if candidate.name == name), None)
        if skill is None:
            return None
        # Defence-in-depth: the skill was discovered by walking the skill
        # roots, so its resolved path must live under one of them. If a future
        # change ever produced a path outside the roots we refuse to serve it
        # rather than risk leaking a file outside the workspace.
        if not self._skill_path_is_confined(skill):
            return None
        return skill

    def _find_skill(self, name: str) -> Skill | None:
        """Load a validated catalog entry without mutating session state."""

        return self.load_skill_by_name(name)

    def bind_session_context(self, session_id: str) -> None:
        """Bind model-invoked context-source tools to the current main run."""

        self._active_session_id = session_id
        if self.config.work_products.enabled:
            self.task_workspace.bind_session(session_id)

    def activate_skill(self, session_id: str, name: str) -> Skill:
        skill = self.load_skill_by_name(name)
        if skill is None:
            raise FileNotFoundError(f"skill not found: {name}")
        self.session_context.activate_skill(session_id, name)
        return skill

    def _load_model_skill(self, name: str) -> str:
        """Model tool implementation confined to visible discovered skills."""

        skill = self.load_skill_by_name(name)
        if skill is None or skill.disable_model_invocation:
            raise FileNotFoundError(f"model-invocable skill not found: {name}")
        if self._active_session_id is not None:
            self.session_context.activate_skill(self._active_session_id, name)
        return expand_skill_for_message(skill)

    def _read_artifact(self, ref: str, start: int = 0, max_chars: int = 8000) -> str:
        """Model tool implementation reading back a spilled tool-output body.

        The context engine replaces bulky historical tool outputs with receipts
        that keep only head/tail excerpts; this closes the loop so the model can
        page through the full body when a follow-up question needs detail the
        receipt dropped. ``ValueError`` (bad ref) and ``FileNotFoundError``
        (missing — including redacted ``artifact_policy="never"`` bodies, which
        are never persisted) are both classified recoverable, so the model gets
        an actionable error instead of a crashed run.
        """

        if start < 0 or max_chars <= 0:
            raise ValueError(f"start must be >= 0 and max_chars > 0, got start={start} max_chars={max_chars}")
        try:
            body = self._artifact_store.read(ref)
        except ArtifactStoreError as error:
            raise ValueError(str(error)) from error
        if body is None:
            raise FileNotFoundError(f"artifact not found: {ref}")
        text = body.decode("utf-8", errors="replace")
        end = min(start + max_chars, len(text))
        return f"[artifact {ref} chars={len(text)} range={start}-{end}]\n{text[start:end]}"

    def _read_model_skill_resource(self, name: str, path: str) -> str:
        """Read one text resource, confined to a model-invocable skill root.

        ``read_file`` deliberately cannot leave the project workspace. User
        skills live under ``~/.lumen/skills``, so their sibling files need
        this narrower capability: the model selects an already-discovered
        skill by name and supplies only a path relative to that skill's base
        directory. Resolving before the containment check also blocks symlink
        escapes.
        """

        skill = self.load_skill_by_name(name)
        if skill is None or skill.disable_model_invocation:
            raise FileNotFoundError(f"model-invocable skill not found: {name}")

        relative = Path(path)
        if relative.is_absolute():
            raise WorkspaceViolation(f"skill resource path must be relative: {path}")
        base_dir = skill.base_dir.resolve()
        resource = (base_dir / relative).resolve(strict=False)
        if not resource.is_relative_to(base_dir):
            raise WorkspaceViolation(f"path escapes skill {name!r}: {path}")
        if not resource.is_file():
            raise FileNotFoundError(f"skill resource not found: {name}/{path}")
        try:
            body = resource.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"skill resource is not UTF-8 text: {name}/{path}") from error
        except OSError as error:
            raise FileNotFoundError(f"cannot read skill resource {name}/{path}: {error}") from error
        return (
            f'<skill-resource skill="{name}" path="{relative.as_posix()}" '
            f'base-directory="{base_dir}">\n{body}\n</skill-resource>'
        )

    def _run_skill_script(
        self,
        skill_name: str,
        script_name: str,
        args: list[str] | None = None,
    ) -> str:
        """Execute one declared script without shell expansion or secret env."""

        skill = self.load_skill_by_name(skill_name)
        if skill is None:
            return f"error: unknown skill {skill_name!r}"
        path = skill.scripts.get(script_name)
        if path is None:
            return f"error: skill {skill_name!r} has no script {script_name!r}"
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            return f"error: skill script is unavailable: {error}"
        base_dir = skill.base_dir.resolve()
        if not resolved.is_relative_to(base_dir):
            return f"error: skill script escapes base directory: {script_name}"
        interpreter = {
            ".py": [sys.executable],
            ".sh": ["sh"],
            ".bash": ["bash"],
        }.get(resolved.suffix.lower())
        if interpreter is None:
            return f"error: unsupported skill script interpreter: {resolved.suffix}"
        prepared = self.sandbox_runner.prepare(
            [*interpreter, str(resolved), *(args or [])],
            cwd=base_dir,
            read_paths=(base_dir,),
        )
        try:
            completed = subprocess.run(
                prepared.argv,
                cwd=base_dir,
                env=prepared.env,
                capture_output=True,
                text=True,
                timeout=self.config.agent.limits.skill_script_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                "error: skill script timed out after "
                f"{self.config.agent.limits.skill_script_timeout_seconds:g}s"
            )
        finally:
            prepared.cleanup()
        output = completed.stdout.rstrip()
        error = completed.stderr.rstrip()
        lines = [f"exit_code: {completed.returncode}"]
        if output:
            lines.extend(("stdout:", output))
        if error:
            lines.extend(("stderr:", error))
        return "\n".join(lines)

    def _skill_path_is_confined(self, skill: Skill) -> bool:
        """Whether ``skill``'s file path lives under a known skill root."""
        path = skill.file_path
        roots: list[Path] = [self.workspace / ".lumen" / "skills"]
        user_root = Path.home() / ".lumen" / "skills"
        roots.append(user_root)
        roots.append(Path(str(files("lumen").joinpath("builtin_skills"))))
        for root in roots:
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            if path == resolved_root or resolved_root in path.parents:
                return True
        return False

    async def select_model(self, name: str) -> None:
        """Switch the active model and rebuild the runtime if it is open.

        Switching rebuilds the ``AgentRuntime`` (and its compaction
        ``ContextManager``) because Pydantic AI binds the model at Agent
        construction. MCP clients, sessions, tool registrations, and history
        are preserved. Raises ``KeyError`` if ``name`` is not configured.

        The switch is transactional: if the new runtime fails to build, the
        previous active model name and runtime are left intact so the app keeps
        working on the old model rather than entering a half-built state.
        """

        if name not in self.model_registry:
            raise KeyError(f"unknown model {name!r}; configured: {self.available_models()}")
        if name == self._active_model_name and self.runtime is not None:
            return
        if self._stack is not None:
            # The runtime is open: rebuild transactionally. We do NOT mutate
            # _active_model_name until the new runtime is confirmed good, so a
            # build failure leaves the old model name and runtime in place.
            await self._rebuild_runtime_for(name)
        self._active_model_name = name

    async def _rebuild_runtime_for(self, name: str) -> None:
        """Atomically swap to ``name``'s runtime, restoring the old one on failure.

        Builds the new runtime on a fresh stack, and only if that succeeds
        closes the old runtime and publishes the new one. If the build raises,
        the old runtime/stack are untouched and the caller leaves the active
        model name unchanged — the app keeps working on the previous model.
        """

        if self._stack is None:
            raise RuntimeError("_rebuild_runtime_for requires the resource stack to be open")
        old_runtime = self.runtime
        old_runtime_stack = self._runtime_stack
        # Tentatively build the new runtime WITHOUT closing the old one first,
        # so a build failure can't leave us runtime-less. _build_runtime
        # refuses to run while a runtime stack is open, so temporarily detach
        # the bookkeeping references and restore them on failure.
        self._runtime_stack = None
        self.runtime = None
        try:
            await self._build_runtime(for_name=name)
        except BaseException:
            # Restore the previous runtime exactly as it was.
            self._runtime_stack = old_runtime_stack
            self.runtime = old_runtime
            raise
        # Build succeeded: publish the new runtime, then close the old stack
        # (which is still referenced by old_runtime_stack) to avoid a leak.
        new_runtime = self.runtime
        new_runtime_stack = self._runtime_stack
        self._runtime_stack = old_runtime_stack
        self.runtime = old_runtime
        await self._close_runtime()
        self.runtime = new_runtime
        self._runtime_stack = new_runtime_stack

    def _load_instructions(self) -> str:
        path = self.config.agent.instructions_file
        self.system_instructions = BASE_INSTRUCTIONS.rstrip()
        policy_sections = [CONTROL_INSTRUCTIONS.strip()]
        if path is None:
            pass
        else:
            try:
                custom = path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ResourceStartupError(f"cannot read instructions file {path}: {error}") from error
            policy_sections.append(f"Project instructions:\n{custom}")
        self.policy_instructions = "\n\n".join(policy_sections)
        base = f"{self.system_instructions}\n\n{self.policy_instructions}\n"
        # Inject the skill catalog (progressive disclosure: only name +
        # description + path, never the body). Model-invocable skills only.
        skills_block = format_skills_for_prompt(self.skills)
        if skills_block:
            return f"{base}\n{skills_block}\n"
        return base

    async def open(self) -> ResourceManager:
        if self._stack is not None:
            return self
        stack = AsyncExitStack()
        await stack.__aenter__()
        # Mark the outer stack active early so ``_build_runtime``'s guard
        # passes and so the except path below can rely on it.
        self._stack = stack
        known_names = set(self.registry.entries) | RESERVED_TOOL_NAMES
        try:
            for bundle in self.mcp_bundles:
                try:
                    # Manual enter + guarded exit (instead of
                    # ``stack.enter_async_context``): the runtime reconnect
                    # path exits/enters the client to rebuild broken sessions,
                    # which can leave the toolset's enter/exit count at zero
                    # when the server stays dead. The guarded exit tolerates
                    # that instead of failing the whole session teardown.
                    await bundle.client.__aenter__()
                    stack.push_async_exit(partial(_guarded_mcp_client_exit, bundle.client))
                    remote_tools = await bundle.client.list_tools()
                except Exception as error:
                    self.mcp_status[bundle.name] = "error"
                    if bundle.config.required:
                        raise ResourceStartupError(
                            f"required MCP server {bundle.name!r} failed: {error}"
                        ) from error
                    self.warnings.append(f"optional MCP server {bundle.name!r} unavailable: {error}")
                    continue
                self.mcp_status[bundle.name] = "ok"
                try:
                    await self.mcp_content.add_server(bundle, bundle.config)
                except Exception as error:
                    self.warnings.append(f"MCP server {bundle.name!r} content discovery failed: {error}")
                raw_names = {tool.name for tool in remote_tools}
                unknown_read_only = set(bundle.config.read_only_tools) - raw_names
                if unknown_read_only:
                    raise ResourceStartupError(
                        f"MCP server {bundle.name!r} has unknown read_only_tools: {sorted(unknown_read_only)}"
                    )
                unknown_tool_risks = set(bundle.config.tool_risks) - raw_names
                if unknown_tool_risks:
                    raise ResourceStartupError(
                        f"MCP server {bundle.name!r} has unknown tool_risks: "
                        f"{sorted(unknown_tool_risks)}; use raw MCP tool names without the "
                        "server prefix"
                    )
                unknown_tool_effects = set(bundle.config.tool_effects) - raw_names
                if unknown_tool_effects:
                    raise ResourceStartupError(
                        f"MCP server {bundle.name!r} has unknown tool_effects: "
                        f"{sorted(unknown_tool_effects)}; use raw MCP tool names without the "
                        "server prefix"
                    )
                unknown_always_loaded = set(bundle.config.always_load_tools) - raw_names
                if unknown_always_loaded:
                    raise ResourceStartupError(
                        f"MCP server {bundle.name!r} has unknown always_load_tools: "
                        f"{sorted(unknown_always_loaded)}"
                    )
                unclassified = raw_names - set(bundle.config.tool_risks) - set(bundle.config.read_only_tools)
                if unclassified:
                    self.warnings.append(
                        f"MCP server {bundle.name!r} unclassified tools default to "
                        f"external_unknown and require approval: {sorted(unclassified)}; "
                        "declare them under tool_risks using raw MCP tool names"
                    )
                for raw_name in raw_names:
                    public_name = bundle.public_name(raw_name)
                    if public_name in RESERVED_TOOL_NAMES:
                        raise DuplicateToolError(
                            f"MCP tool name conflicts with reserved control tool: {public_name}"
                        )
                    if public_name in known_names:
                        raise DuplicateToolError(f"MCP tool name conflicts with another tool: {public_name}")
                    known_names.add(public_name)
                    if public_name not in self.policy.always_deny:
                        self.tool_metadata[public_name] = {
                            "origin": f"mcp:{bundle.name}",
                            "risk": bundle.risk_for(public_name).value,
                            "effect": bundle.effect_for(public_name).value,
                        }
                    remote_tool = next(tool for tool in remote_tools if tool.name == raw_name)
                    self._remote_tool_schema_documents.append(
                        {
                            "name": public_name,
                            "description": getattr(remote_tool, "description", None) or "",
                            "parameters": getattr(
                                remote_tool,
                                "inputSchema",
                                getattr(remote_tool, "input_schema", {}),
                            ),
                            "returns": getattr(
                                remote_tool,
                                "outputSchema",
                                getattr(remote_tool, "output_schema", None),
                            ),
                            "origin": f"mcp:{bundle.name}",
                            "deferred": bundle.is_deferred(public_name),
                        }
                    )
                    if public_name not in self.policy.always_deny:
                        parameters = getattr(
                            remote_tool,
                            "inputSchema",
                            getattr(remote_tool, "input_schema", {}),
                        )
                        self.capability_gateway.register(
                            CapabilityDescriptor(
                                name=public_name,
                                description=getattr(remote_tool, "description", None) or "",
                                parameters=cast(dict[str, Any], parameters),
                                origin=f"mcp:{bundle.name}",
                                risk=bundle.risk_for(public_name).value,
                                effect_kind=bundle.effect_for(public_name),
                                requires_approval=bundle.requires_approval(public_name),
                                timeout_seconds=self.config.agent.limits.tool_timeout_seconds,
                            ),
                            partial(self._invoke_live_mcp, bundle, public_name),
                        )
                self._active_toolsets.append(bundle.toolset)

            configured_permissions = self.policy.always_allow | self.policy.always_deny
            unknown_permissions = configured_permissions - known_names
            if unknown_permissions:
                raise ResourceStartupError(
                    f"permission rules reference unknown tools: {sorted(unknown_permissions)}"
                )

            # Build the runtime on its own inner stack so model switches can
            # tear it down without disconnecting MCP clients.
            await self._build_runtime()
        except BaseException:
            await self.close()
            raise
        return self

    async def _build_runtime(self, *, for_name: str | None = None) -> None:
        """Construct the AgentRuntime for ``for_name`` (or the active model).

        ``for_name`` lets the transactional rebuild construct a candidate
        runtime for a different model without first mutating the active name.
        Raises ``RuntimeError`` if called while a runtime stack is already
        open; callers must close the previous one first via ``_close_runtime``.
        """

        if self._stack is None:
            raise RuntimeError("_build_runtime requires the resource stack to be open")
        if self._runtime_stack is not None:
            raise RuntimeError("runtime stack already open; close it before rebuilding")
        model_name = for_name if for_name is not None else self._active_model_name
        model_cfg = self.model_registry[model_name]
        runtime_instructions = (
            f"{self.instructions.rstrip()}\n\n"
            "<runtime_identity>\n"
            f"framework: {FRAMEWORK_NAME}\n"
            f"active_model_name: {model_name}\n"
            f"active_model_id: {model_cfg.id}\n"
            "Report these fields exactly when the user asks about framework or model identity.\n"
            "</runtime_identity>\n"
        )
        runtime_stack = AsyncExitStack()
        await runtime_stack.__aenter__()
        try:
            active_model = build_model(model_cfg)
            self.memory_manager.configure_learning(extractor=self._memory_extractor(active_model))
            runtime_tools = list(self.local_tools)
            runtime_metadata = dict(self.tool_metadata)
            if self.config.agents.enabled:
                runtime_tools.extend(
                    [
                        Tool(
                            self.agent_orchestrator.spawn_agent,
                            name="spawn_agent",
                            description=(
                                "Spawn a persistent depth-one Agent Thread and return immediately. "
                                "Use explorer for read-only research and worker for isolated changes."
                            ),
                            sequential=True,
                            requires_approval=False,
                        ),
                        Tool(
                            self.agent_orchestrator.send_message,
                            name="send_message",
                            description="Queue context for an Agent without starting a new turn.",
                            sequential=True,
                        ),
                        Tool(
                            self.agent_orchestrator.followup_task,
                            name="followup_task",
                            description="Continue an existing Agent with its durable context.",
                            sequential=True,
                        ),
                        Tool(
                            self.agent_orchestrator.wait_agent,
                            name="wait_agent",
                            description="Wait for Agent progress, completion, failure, or coordination.",
                            sequential=True,
                        ),
                        Tool(
                            self.agent_orchestrator.interrupt_agent,
                            name="interrupt_agent",
                            description="Interrupt an active Agent while preserving its thread state.",
                            sequential=True,
                        ),
                        Tool(
                            self.agent_orchestrator.list_agents,
                            name="list_agents",
                            description="List durable Agent Threads in the current root Session.",
                            sequential=True,
                        ),
                        Tool(
                            self.agent_orchestrator.close_agent,
                            name="close_agent",
                            description="Resolve and close an Agent Thread after its work is handled.",
                            sequential=True,
                        ),
                    ]
                )
                for agent_tool in AGENT_TOOL_NAMES:
                    runtime_metadata[agent_tool] = dict(self.tool_metadata[agent_tool])
                if self.config.delegation.enabled and self.child_run_manager is not None:
                    runtime_tools.extend(
                        [
                            Tool(
                                self.child_run_manager.spawn_child,
                                name="spawn_child",
                                description="Deprecated alias for spawn_agent.",
                                sequential=True,
                            ),
                            Tool(
                                self.child_run_manager.wait_children,
                                name="wait_children",
                                description="Deprecated alias for wait_agent.",
                                sequential=True,
                            ),
                            Tool(
                                self.child_run_manager.cancel_child,
                                name="cancel_child",
                                description="Deprecated alias for interrupt_agent.",
                                sequential=True,
                            ),
                        ]
                    )
                    for child_tool in CHILD_TOOL_NAMES:
                        runtime_metadata[child_tool] = dict(self.tool_metadata[child_tool])
            self.runtime = AgentRuntime(
                model=active_model,
                tools=runtime_tools,
                toolsets=list(self._active_toolsets),
                instructions=runtime_instructions,
                system_instructions=self.system_instructions,
                policy_instructions=self.policy_instructions,
                limits=self.config.agent.limits,
                tool_metadata=runtime_metadata,
                model_settings=cast(ModelSettings, model_cfg.settings),
                context_engine=ContextEngine(
                    config=self.config.context,
                    model=active_model,
                    model_id=model_cfg.id,
                    model_config=model_cfg,
                    artifact_root=str(Path.home() / ".lumen" / "artifacts"),
                    memory=self.memory_manager,
                ),
                tool_schema_documents=self._remote_tool_schema_documents,
                active_skill_documents=self.active_skill_documents,
                retrieved_context_documents=self.retrieved_context_documents,
                active_work_product_documents=self.active_work_product_documents,
                work_completion_issues=self.completion_issues,
                usage_enricher=self.enrich_usage,
                effect_recorder=self.task_workspace.record_tool_effect,
                work_event_drain=self.task_workspace.drain_events,
                bind_session_context=self.bind_session_context,
                clarification_loader=self.pending_clarification,
                clarification_setter=self.request_clarification,
                clarification_clearer=self.clear_clarification,
                hooks=self.hooks,
            )
            await runtime_stack.enter_async_context(self.runtime.agent)
        except BaseException:
            await runtime_stack.aclose()
            self.runtime = None
            raise
        self._runtime_stack = runtime_stack
        self.memory_manager.start()

    async def _close_runtime(self) -> None:
        """Tear down the runtime-only stack, leaving MCP clients connected."""

        if self._runtime_stack is not None:
            await self._runtime_stack.aclose()
            self._runtime_stack = None
        self.runtime = None

    async def close(self) -> None:
        if self.live_manager is not None:
            await self.live_manager.close()
        await self.agent_orchestrator.shutdown()
        if self._stack is not None:
            await self._close_runtime()
            await self._stack.aclose()
            self._stack = None
            self.runtime = None
            self._active_toolsets = []
        await self.memory_manager.close()

    async def __aenter__(self) -> ResourceManager:
        return await self.open()

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    def summary(self) -> dict[str, Any]:
        return {
            "agent": self.config.agent.name,
            "model": self.active_model_config().id,
            "active_model": self._active_model_name,
            "available_models": self.available_models(),
            "workspace": str(self.workspace),
            "session_directory": str(self.config.sessions.directory),
            "project_trusted": self.config.project_trusted,
            "config_sources": [
                source.as_dict() if hasattr(source, "as_dict") else str(source)
                for source in self.config.config_sources
            ],
            "tools": sorted(self.tool_metadata),
            "mcp_status": dict(self.mcp_status),
            "mcp_approvals": list(self.config.mcp_diagnostics),
            "warnings": [*self.config.config_warnings, *self.warnings],
            "hooks": self.hooks.summary(),
            "live_enabled": self.config.live.enabled,
        }

    def hook_summary(self) -> list[dict[str, object]]:
        return self.hooks.summary()

    def mcp_summary(self) -> list[dict[str, object]]:
        """Connection and schema-loading status for the `/mcp` surface."""

        rows: list[dict[str, object]] = []
        for bundle in self.mcp_bundles:
            server = self.config.mcp_servers[bundle.name]
            documents = [
                document
                for document in self._remote_tool_schema_documents
                if document.get("origin") == f"mcp:{bundle.name}"
            ]
            rows.append(
                {
                    "name": bundle.name,
                    "status": self.mcp_status.get(bundle.name, "unknown"),
                    "tools": len(documents),
                    "deferred": sum(1 for document in documents if document.get("deferred") is True),
                    "always_loaded": sum(1 for document in documents if document.get("deferred") is not True),
                    "scope": server.source_scope,
                    "approval": server.approval_status,
                }
            )
        active_names = {bundle.name for bundle in self.mcp_bundles}
        for diagnostic in self.config.mcp_diagnostics:
            if diagnostic.get("name") in active_names:
                continue
            rows.append(
                {
                    "name": diagnostic.get("name", "unknown"),
                    "status": "not_started",
                    "tools": 0,
                    "deferred": 0,
                    "always_loaded": 0,
                    "scope": diagnostic.get("scope", "unknown"),
                    "approval": diagnostic.get("approval", "unknown"),
                }
            )
        return rows

    def active_skill_documents(self, session_id: str) -> tuple[dict[str, object], ...]:
        return self.session_context.resolve_documents(session_id).skill_documents

    def retrieved_context_documents(self, session_id: str) -> tuple[dict[str, object], ...]:
        return self.session_context.resolve_documents(session_id).resource_documents

    def active_work_product_documents(self, session_id: str) -> tuple[dict[str, object], ...]:
        return (
            *self.task_workspace.context_documents(session_id),
            *self.agent_orchestrator.context_documents(session_id),
        )

    def live_context_documents(self, session_id: str) -> tuple[dict[str, object], ...]:
        """Return a bounded canonical projection; never include raw audio or secrets."""

        loaded = self.session_repository.load(session_id)

        def assistant_text(turn: Any) -> str:
            chunks: list[str] = []
            for message in turn.messages:
                if not isinstance(message, ModelResponse):
                    continue
                chunks.extend(part.content for part in message.parts if isinstance(part, TextPart))
            return "".join(chunks) or turn.partial_text or ""

        recent_turns = [
            {
                "kind": "conversation_turn",
                "channel": turn.channel,
                "user": turn.user_input,
                "assistant": assistant_text(turn),
                "status": turn.status,
            }
            for turn in loaded.turns[-8:]
        ]
        return tuple(
            [
                {
                    "kind": "session",
                    "plan": loaded.plan.model_dump(mode="json"),
                    "recent_turns": recent_turns,
                },
                *self.active_skill_documents(session_id),
                *self.retrieved_context_documents(session_id),
                *self.active_work_product_documents(session_id),
            ]
        )

    def bind_live_session(self, session_id: str, execution_id: str) -> None:
        """Bind stateful tools to one short-lived Live execution."""

        self.bind_session_context(session_id)
        self.agent_orchestrator.bind_root_run(
            session_id,
            execution_id,
            approval_mode=str(self.config.permissions.default_mode),
        )

    async def _invoke_live_mcp(
        self,
        bundle: McpToolsetBundle,
        public_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Execute one MCP capability through its existing resilient Adapter."""

        model = cast(Model, build_model(self.active_model_config()))
        context = RunContext(
            deps=None,
            model=model,
            usage=RunUsage(),
            tool_call_approved=True,
            tool_name=public_name,
        )
        tools = await bundle.toolset.get_tools(context)
        tool = tools.get(public_name)
        if tool is None:
            raise RuntimeError(f"MCP capability is no longer available: {public_name}")
        raw_validated = tool.args_validator.validate_python(arguments)
        if not isinstance(raw_validated, dict):
            raise TypeError("MCP capability arguments did not validate to an object")
        validated = cast(dict[str, Any], raw_validated)
        return await bundle.toolset.call_tool(public_name, validated, context, tool)

    def completion_issues(self, session_id: str) -> tuple[str, ...]:
        return tuple(
            [
                *self.task_workspace.completion_issues(session_id),
                *self.agent_orchestrator.completion_issues(session_id),
            ]
        )

    def enrich_usage(
        self,
        session_id: str,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        summary = self.agent_orchestrator.usage_summary(
            session_id,
            self.agent_orchestrator.bound_root_run_id,
        )
        raw_totals = summary.get("total", {})
        totals = cast(dict[str, object], raw_totals) if isinstance(raw_totals, dict) else {}
        combined = dict(usage)
        for key, value in totals.items():
            old = combined.get(key, 0)
            if (
                not isinstance(value, bool)
                and isinstance(value, int | float)
                and not isinstance(old, bool)
                and isinstance(old, int | float)
            ):
                combined[key] = old + value
        combined["agents"] = summary.get("by_agent", {})
        return combined

    async def _record_agent_evidence(
        self,
        receipt: EvidenceReceipt,
        plan_step_id: str | None,
        criterion_ids: tuple[str, ...],
    ) -> None:
        if self.runtime is None:
            return
        self.runtime.controller.record_evidence(receipt)
        if plan_step_id is not None:
            await self.runtime.controller.attach_executor_evidence(
                plan_step_id,
                receipt.id,
                list(criterion_ids),
            )

    def pending_clarification(self, session_id: str) -> Any:
        return self.session_context.load(session_id).pending_clarification

    def request_clarification(
        self,
        session_id: str,
        question: str,
        choices: tuple[str, ...],
        related_plan_step: str | None,
    ) -> Any:
        return self.session_context.new_clarification(
            session_id,
            question=question,
            choices=choices,
            related_plan_step=related_plan_step,
        )

    def clear_clarification(self, session_id: str) -> None:
        self.session_context.set_clarification(session_id, None)

    def context_source_summary(self, session_id: str) -> list[dict[str, str]]:
        state = self.session_context.load(session_id)
        return [
            *(
                {
                    "kind": "skill",
                    "reference": item.name,
                    "revision": item.revision,
                    "status": (
                        "available"
                        if self.session_context.artifacts.read(item.body_artifact_ref) is not None
                        else "unavailable"
                    ),
                }
                for item in state.active_skills
            ),
            *(
                {
                    "kind": "resource",
                    "reference": item.reference,
                    "revision": item.revision,
                    "status": (
                        "available"
                        if self.session_context.artifacts.read(item.body_artifact_ref) is not None
                        else "unavailable"
                    ),
                }
                for item in state.active_resources
            ),
        ]

    def mcp_resource_summary(self, session_id: str | None = None) -> list[dict[str, object]]:
        active: set[str] = (
            {item.reference for item in self.session_context.load(session_id).active_resources}
            if session_id is not None
            else set[str]()
        )
        return [
            {
                "reference": item.reference,
                "server": item.server,
                "uri": item.uri,
                "name": item.name,
                "description": item.description,
                "mime_type": item.mime_type,
                "active": item.reference in active,
            }
            for item in self.mcp_content.resources
        ]

    async def activate_mcp_resource(self, session_id: str, reference: str) -> dict[str, object]:
        state = await self.session_context.activate_resource(session_id, reference)
        item = next(resource for resource in state.active_resources if resource.reference == reference)
        return {
            "reference": item.reference,
            "server": item.server,
            "uri": item.uri,
            "revision": item.revision,
        }

    def deactivate_context_source(self, session_id: str, kind: str, reference: str) -> None:
        self.session_context.deactivate(session_id, kind, reference)

    def mcp_prompt_summary(self) -> list[dict[str, object]]:
        return [
            {
                "reference": item.reference,
                "server": item.server,
                "name": item.name,
                "description": item.description,
                "arguments": item.arguments,
            }
            for item in self.mcp_content.prompts
        ]

    async def render_mcp_prompt(self, reference: str, arguments: dict[str, str]) -> str:
        body = await self.mcp_content.render_prompt(reference, arguments)
        return render_mcp_prompt(reference=reference, body=body)
