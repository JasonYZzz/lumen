from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, cast

from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset

from lumen.branding import FRAMEWORK_NAME
from lumen.config import AppConfig, ModelSettingsConfig
from lumen.context import ContextEngine
from lumen.context.memory import MemoryManager, SQLiteMemoryRepository
from lumen.mcp_tools import McpToolsetBundle, build_mcp_toolset
from lumen.models import build_model
from lumen.runtime import CONTROL_INSTRUCTIONS, AgentRuntime
from lumen.sessions import SessionRepository
from lumen.skills import Skill, SkillLoader, expand_skill_for_message, format_skills_for_prompt
from lumen.tools.builtin import build_builtin_specs
from lumen.tools.registry import DuplicateToolError, PermissionPolicy, ToolRegistry, load_plugin_specs
from lumen.tools.spec import Risk, ToolSpec
from lumen.tools.workspace import WorkspaceViolation

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
Keep user-facing explanations concise. Do not reveal private chain-of-thought;
provide only brief useful rationale.
When the task is complete, answer the user directly.
"""

# Names the runtime reserves for control tools; the resource manager refuses to
# register any builtin/plugin/MCP tool that collides so a misconfigured plugin
# cannot shadow the planning and progress channel.
CONTROL_TOOL_NAMES = frozenset({"set_plan", "update_step", "report_progress"})


class ResourceStartupError(RuntimeError):
    """Raised when configured runtime resources cannot be initialized."""


class ResourceManager:
    def __init__(self, config: AppConfig, *, workspace: str | Path) -> None:
        self.config = config
        self.workspace = Path(workspace).expanduser().resolve()
        self.warnings: list[str] = []
        # Skill discovery runs before _load_instructions because the latter
        # injects the skill catalog into the system prompt. Skills are
        # disabled by config when the user sets ``agent.skills_enabled: false``.
        self.skills: list[Skill] = []
        if config.agent.skills_enabled:
            loader = SkillLoader(self.workspace)
            self.skills = loader.discover()
            self.warnings.extend(loader.warnings)
        self.instructions = self._load_instructions()
        self.policy = PermissionPolicy(config.permissions)
        self.registry = ToolRegistry(self.workspace)
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
        builtins = {
            spec.name: spec
            for spec in build_builtin_specs(
                self.workspace, max_timeout=config.agent.limits.tool_timeout_seconds
            )
        }
        for name in config.tools.builtins:
            spec = builtins[name]
            self.registry.add(spec, origin="builtin")
        for plugin in config.tools.plugins:
            self.registry.add_many(
                load_plugin_specs(plugin, search_path=config.config_path.parent),
                origin=f"plugin:{plugin.module}",
            )
        self.local_tools = self.registry.build_local_tools(
            self.policy, default_timeout=config.agent.limits.tool_timeout_seconds
        )
        self.mcp_bundles: list[McpToolsetBundle] = [
            build_mcp_toolset(
                name,
                server,
                self.policy,
                cwd=self.workspace,
                timeout=config.agent.limits.tool_timeout_seconds,
            )
            for name, server in config.mcp_servers.items()
        ]
        self.session_repository = SessionRepository(config.sessions.directory)
        self.tool_metadata: dict[str, dict[str, str]] = {
            name: {"origin": entry.origin, "risk": entry.spec.risk.value}
            for name, entry in self.registry.entries.items()
            if name not in self.policy.always_deny
        }
        # Control tools are always visible, marked so the TUI can render them
        # distinctly, and excluded from the optional read-only class.
        for control_name in CONTROL_TOOL_NAMES:
            self.tool_metadata[control_name] = {"origin": "control", "risk": "read", "control": "true"}
        self.mcp_status: dict[str, str] = {name: "connecting" for name in config.mcp_servers}
        self.runtime: AgentRuntime | None = None
        # Model registry derived from agent.model (legacy) or agent.models.
        self.model_registry: dict[str, ModelSettingsConfig] = config.agent.model_registry()
        self._active_model_name: str = config.agent.default_model_name()
        self._active_toolsets: list[AbstractToolset[None]] = []
        self._remote_tool_schema_documents: list[dict[str, Any]] = []
        # MCP clients live on the outer stack so model switches can rebuild the
        # runtime without reconnecting remote services. The runtime agent lives
        # on its own inner stack so it can be torn down in isolation.
        self._stack: AsyncExitStack | None = None
        self._runtime_stack: AsyncExitStack | None = None

    # -- model registry ----------------------------------------------------

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

        ``memory.use`` defaults to True (explicit recall on); auto-learning stays
        off until M6. The SQLite file lives under ``~/.lumen/state/`` so it is
        shared across sessions and worktrees by canonical home, not repo path.
        """

        store_path = Path.home() / ".lumen" / "state" / "memory.sqlite3"
        repository = SQLiteMemoryRepository(store_path)
        return MemoryManager(repository, use=True)

    def available_models(self) -> list[str]:
        """Return the sorted logical names of all configured models."""

        return sorted(self.model_registry)

    def get_skill(self, name: str) -> Skill | None:
        """Look up a skill by name (used by the ``/skill:<name>`` command).

        Searches all discovered skills regardless of
        ``disable_model_invocation`` — a manually-invoked skill is always
        reachable via the slash command even if it's hidden from the model's
        automatic catalog.
        """

        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

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

        skill = self.get_skill(name)
        if skill is None:
            return None
        # Defence-in-depth: the skill was discovered by walking the skill
        # roots, so its resolved path must live under one of them. If a future
        # change ever produced a path outside the roots we refuse to serve it
        # rather than risk leaking a file outside the workspace.
        if not self._skill_path_is_confined(skill):
            return None
        return skill

    def _load_model_skill(self, name: str) -> str:
        """Model tool implementation confined to visible discovered skills."""

        skill = self.load_skill_by_name(name)
        if skill is None or skill.disable_model_invocation:
            raise FileNotFoundError(f"model-invocable skill not found: {name}")
        return expand_skill_for_message(skill)

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

    def _skill_path_is_confined(self, skill: Skill) -> bool:
        """Whether ``skill``'s file path lives under a known skill root."""
        path = skill.file_path
        roots: list[Path] = [self.workspace / ".lumen" / "skills"]
        user_root = Path.home() / ".lumen" / "skills"
        roots.append(user_root)
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
        if path is None:
            base = f"{BASE_INSTRUCTIONS.rstrip()}\n\n{CONTROL_INSTRUCTIONS.strip()}\n"
        else:
            try:
                custom = path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ResourceStartupError(f"cannot read instructions file {path}: {error}") from error
            base = (
                f"{BASE_INSTRUCTIONS.rstrip()}\n\n"
                f"{CONTROL_INSTRUCTIONS.strip()}\n\nProject instructions:\n{custom}\n"
            )
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
        known_names = set(self.registry.entries) | CONTROL_TOOL_NAMES
        try:
            for bundle in self.mcp_bundles:
                try:
                    await stack.enter_async_context(bundle.client)
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
                unclassified = raw_names - set(bundle.config.tool_risks) - set(bundle.config.read_only_tools)
                if unclassified:
                    self.warnings.append(
                        f"MCP server {bundle.name!r} unclassified tools default to "
                        f"external_unknown and require approval: {sorted(unclassified)}; "
                        "declare them under tool_risks using raw MCP tool names"
                    )
                for raw_name in raw_names:
                    public_name = bundle.public_name(raw_name)
                    if public_name in CONTROL_TOOL_NAMES:
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
                        }
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
            self.runtime = AgentRuntime(
                model=build_model(model_cfg),
                tools=self.local_tools,
                toolsets=list(self._active_toolsets),
                instructions=runtime_instructions,
                limits=self.config.agent.limits,
                tool_metadata=self.tool_metadata,
                model_settings=cast(ModelSettings, model_cfg.settings),
                context_engine=ContextEngine(
                    config=self.config.context,
                    model=build_model(model_cfg),
                    model_id=model_cfg.id,
                    artifact_root=str(Path.home() / ".lumen" / "artifacts"),
                    memory=self._build_memory_manager(),
                ),
                tool_schema_documents=self._remote_tool_schema_documents,
            )
            await runtime_stack.enter_async_context(self.runtime.agent)
        except BaseException:
            await runtime_stack.aclose()
            self.runtime = None
            raise
        self._runtime_stack = runtime_stack

    async def _close_runtime(self) -> None:
        """Tear down the runtime-only stack, leaving MCP clients connected."""

        if self._runtime_stack is not None:
            await self._runtime_stack.aclose()
            self._runtime_stack = None
        self.runtime = None

    async def close(self) -> None:
        if self._stack is not None:
            await self._close_runtime()
            await self._stack.aclose()
            self._stack = None
            self.runtime = None
            self._active_toolsets = []

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
            "tools": sorted(self.tool_metadata),
            "mcp_status": dict(self.mcp_status),
            "warnings": list(self.warnings),
        }
