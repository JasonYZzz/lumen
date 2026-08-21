from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigLoadError(ValueError):
    """Raised when an agent configuration cannot be loaded safely."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelTokenizerConfig(StrictModel):
    """Offline tokenizer selection for one model capability profile."""

    kind: Literal["auto", "conservative", "tiktoken"] = "auto"
    encoding: str | None = None

    @model_validator(mode="after")
    def validate_encoding(self) -> ModelTokenizerConfig:
        if self.kind == "tiktoken" and not self.encoding:
            raise ValueError("tokenizer.encoding is required when kind is 'tiktoken'")
        if self.kind != "tiktoken" and self.encoding is not None:
            raise ValueError("tokenizer.encoding is only valid when kind is 'tiktoken'")
        return self


class ModelContextOverride(StrictModel):
    """Per-model context capability selection, independent of wire protocol."""

    profile: str | None = None
    window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    tokenizer: ModelTokenizerConfig | None = None
    soft_limit_tokens: int | None = Field(default=None, gt=0)
    keep_recent_tokens: int | None = Field(default=None, gt=0)


class ModelSettingsConfig(StrictModel):
    id: str
    api_key_env: str | None = None
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    base_url: str | None = None
    # OpenAI-compatible API path selector. ``chat`` = /chat/completions,
    # ``responses`` = /responses (阿里云百炼 Responses API, OpenAI Responses API).
    # Accepts aliases used by external tools (openai-completions, openai-responses,
    # chat-completions) for paste-friendly config. When unset, the legacy default
    # applies: a custom base_url selects chat, the default OpenAI endpoint selects
    # responses.
    api: Literal["chat", "responses", "openai-completions", "openai-responses", "chat-completions"] | None = (
        None
    )
    settings: dict[str, Any] = Field(default_factory=dict)
    context: ModelContextOverride = Field(default_factory=ModelContextOverride)


class LimitsConfig(StrictModel):
    #: Per-run caps passed to pydantic_ai's ``UsageLimits``. The defaults are
    #: sized for multi-step planned tasks: an 8-step plan averages 2-3 LLM
    #: calls per step (planning + tool decision + summary) = 16-24 requests,
    #: and reflection/retry easily pushes that to 40+. 50 gives headroom
    #: without inviting runaway spend. Raise via agent.yaml when needed.
    request_count: int = Field(default=50, ge=1)
    tool_calls: int = Field(default=100, ge=0)
    #: There is intentionally NO ``total_tokens`` cap. coding-agent doesn't set
    #: one either — context growth is managed by :class:`ContextManager`'s
    #: soft-limit auto-compaction, which summarizes old history before the
    #: provider's context window fills. A cumulative-token hard wall would
    #: halt long agentic loops mid-task; the compaction system is the primary
    #: defense against context overflow. (Field removed entirely; old configs
    #: that set ``total_tokens`` will fail StrictModel's extra=forbid — remove
    #: the line from agent.yaml.)
    tool_timeout_seconds: float = Field(default=60.0, gt=0)
    skill_script_timeout_seconds: float = Field(default=30.0, gt=0)
    #: Tool-call scheduling policy. ``parallel_safe`` only overlaps tools with
    #: an explicit ``ToolConcurrency.PARALLEL_SAFE`` declaration, while
    #: ``parallel`` also allows exclusive tools to overlap.
    #: The default preserves the pre-M6 execution order.
    parallel_tool_calls: Literal["sequential", "parallel_safe", "parallel"] = "sequential"


class DelegationConfig(StrictModel):
    """Bounded, read-only subagent execution policy.

    Delegation is opt-in because every delegated task consumes additional model
    requests. Child agents receive only tools classified as ``read`` and never
    receive the delegation tool itself, preventing recursive fan-out.
    """

    enabled: bool = False
    research_enabled: bool = True
    worktree_enabled: bool = False
    worktree_root: Path = Path("~/.lumen/worktrees")
    max_concurrency: int = Field(default=3, ge=1, le=8)
    request_count: int = Field(default=10, ge=1, le=50)
    tool_calls: int = Field(default=20, ge=0, le=100)
    timeout_seconds: float = Field(default=180.0, gt=0, le=1800)


class AgentsConfig(StrictModel):
    """Native, session-scoped multi-agent orchestration policy."""

    enabled: bool = True
    autonomy: Literal["adaptive", "explicit", "disabled"] = "adaptive"
    max_depth: Literal[1] = 1
    max_concurrency: int = Field(default=3, ge=1, le=8)
    max_agents_per_run: int = Field(default=8, ge=1, le=32)
    default_agent: str = Field(default="default", pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    recovery: Literal["safe"] = "safe"
    worktree_root: Path = Path("~/.lumen/worktrees")
    request_count: int = Field(default=10, ge=1, le=50)
    tool_calls: int = Field(default=20, ge=0, le=100)
    timeout_seconds: float = Field(default=180.0, gt=0, le=1800)


class AgentSection(StrictModel):
    """Agent configuration with single- or multi-model support.

    Two mutually exclusive forms are accepted:

    1. Legacy single-model: ``model: {id: openai:gpt-5, api_key_env: ...}``.
    2. Multi-model: ``models: {name: {id: ..., ...}, ...}`` with an optional
       ``default_model: <name>`` selecting the active one at startup.

    Mixing ``model:`` and ``models:`` is rejected so the active source is
    unambiguous. The runtime exposes a model registry derived from whichever
    form is in use, and lets the TUI / CLI switch the active entry at runtime.
    """

    name: str = "lumen"
    instructions_file: Path | None = None
    model: ModelSettingsConfig | None = None
    models: dict[str, ModelSettingsConfig] = Field(default_factory=dict[str, ModelSettingsConfig])
    default_model: str | None = None
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    #: Whether to discover Agent Skills (SKILL.md files) from
    #: ``<workspace>/.lumen/skills/`` and ``~/.lumen/skills/`` and
    #: inject their catalog into the system prompt. Set to ``false`` to
    #: disable skill discovery entirely.
    skills_enabled: bool = True
    #: Ship builtin skills with the wheel but keep activation opt-in so
    #: existing installations retain their exact discovered catalog.
    builtin_skills_enabled: bool = False

    @model_validator(mode="after")
    def validate_model_fields(self) -> AgentSection:
        has_single = self.model is not None
        has_multi = bool(self.models)
        if has_single and has_multi:
            raise ValueError("specify either 'agent.model' or 'agent.models', not both")
        if not has_single and not has_multi:
            raise ValueError("agent configuration must define 'model' or 'models'")
        if has_multi:
            if self.default_model is not None and self.default_model not in self.models:
                raise ValueError(
                    f"default_model {self.default_model!r} is not in agent.models: {sorted(self.models)}"
                )
        elif self.default_model is not None:
            # Single-model form has no name; a default_model hint is meaningless.
            raise ValueError("default_model requires the multi-model 'models:' form")
        return self

    def model_registry(self) -> dict[str, ModelSettingsConfig]:
        """Return a name → config mapping, normalising the single-model form."""

        if self.models:
            return dict(self.models)
        assert self.model is not None  # validated above
        # The single-model form has no logical name; use the model id without
        # provider prefix as a stable default so callers can still look it up.
        bare = self.model.id.split(":", 1)[-1]
        return {bare: self.model}

    def default_model_name(self) -> str:
        """The active model name at startup (first key when unspecified)."""

        registry = self.model_registry()
        if self.default_model is not None:
            return self.default_model
        return next(iter(registry))


class PluginConfig(StrictModel):
    module: str
    factory: str = "create_tools"
    # Runtime provenance used to resolve importable plugin modules. It is
    # deliberately excluded from serialization and may be injected by the
    # layered resolver for each individual declaration.
    source_dir: Path | None = Field(default=None, exclude=True, repr=False)


class HookConfig(StrictModel):
    event: Literal[
        "pre_tool_use",
        "post_tool_use",
        "user_prompt_submit",
        "stop",
        "notification",
    ]
    matcher: str = "*"
    command: list[str] | None = None
    module: str | None = None
    factory: str = "hook"
    timeout: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def validate_runner(self) -> HookConfig:
        if (self.command is None) == (self.module is None):
            raise ValueError("hook must define exactly one of 'command' or 'module'")
        if self.command is not None and not self.command:
            raise ValueError("hook command must not be empty")
        return self


class WebSearchConfig(StrictModel):
    provider: Literal["tavily", "brave"]
    api_key_env: str
    max_results: int = Field(default=8, ge=1, le=20)


class WebToolsConfig(StrictModel):
    fetch_timeout_seconds: float = Field(default=20.0, gt=0)
    fetch_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    search: WebSearchConfig | None = None


class ToolsConfig(StrictModel):
    builtins: list[
        Literal[
            "read_file",
            "list_directory",
            "search_text",
            "web_fetch",
            "write_file",
            "edit_file",
            "run_command",
        ]
    ] = Field(default_factory=lambda: ["read_file", "list_directory", "search_text", "web_fetch"])
    plugins: list[PluginConfig] = Field(default_factory=list[PluginConfig])
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)


class OAuthConfig(StrictModel):
    client_id: str | None = None
    client_secret: str | None = Field(default=None, exclude=True, repr=False)
    scopes: list[str] = Field(default_factory=list[str])
    credential_file: str = "mcp_oauth/{server}.json"
    callback_port: int | None = Field(default=None, ge=1, le=65535)
    callback_timeout: float = Field(default=300.0, gt=0)


class McpServerConfig(StrictModel):
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    load_resources: bool = True
    load_prompts: bool = True
    oauth: OAuthConfig | None = None
    #: Legacy allow-list of tool names (without the server prefix) treated as
    #: side-effect-free reads. Deprecated in favour of ``tool_risks``, which
    #: classifies a tool as read/write/execute. Kept for backwards
    #: compatibility; emits a deprecation warning at build time.
    read_only_tools: list[str] = Field(default_factory=list)
    #: Per-tool risk declarations, keyed by the tool's raw name (without the
    #: ``<server>_`` prefix). Accepted values: ``read``, ``write``,
    #: ``execute``. A tool listed here is classified to that risk; any tool
    #: not listed defaults to ``external_unknown`` (always confirm), so a
    #: forgotten ``delete_record`` / ``send_email`` can never silently run in
    #: auto mode.
    tool_risks: dict[str, Literal["read", "write", "execute"]] = Field(
        default_factory=dict[str, Literal["read", "write", "execute"]]
    )
    #: Independent effect declarations used for sequencing and verification.
    #: Undeclared remote tools remain ``unknown`` even when their approval risk
    #: is explicitly configured.
    tool_effects: dict[
        str,
        Literal["observe", "mutation", "execution", "external_action", "unknown"],
    ] = Field(default_factory=dict)
    required: bool = True
    #: Hide MCP schemas until tool search discovers them. Names/descriptions
    #: remain searchable; listed exceptions stay fully visible.
    defer_tools: bool = True
    always_load_tools: list[str] = Field(default_factory=list)
    source_scope: str = Field(default="explicit", exclude=True, repr=False)
    source_path: Path | None = Field(default=None, exclude=True, repr=False)
    definition_fingerprint: str | None = Field(default=None, exclude=True, repr=False)
    approval_status: str = Field(default="automatic", exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> McpServerConfig:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires command")
            if self.url is not None:
                raise ValueError("stdio transport does not accept url")
            if self.headers:
                raise ValueError("stdio transport does not accept headers")
        else:
            if not self.url:
                raise ValueError("streamable_http transport requires url")
            if self.command is not None or self.args:
                raise ValueError("streamable_http transport does not accept command or args")
            if self.env:
                raise ValueError("streamable_http transport does not accept env")
        if self.oauth is not None and self.transport != "streamable_http":
            raise ValueError("MCP OAuth requires streamable_http transport")
        return self


class PermissionsConfig(StrictModel):
    always_allow: list[str] = Field(default_factory=list)
    always_deny: list[str] = Field(default_factory=list)
    #: Session approval mode. Collaboration/Plan is configured separately.
    #: Undeclared remote (MCP) tools default to ``external_unknown`` and always
    #: prompt. Toggle live via ``/mode`` or ``Shift+Tab`` without rebuilding
    #: the runtime.
    default_mode: Literal["manual", "accept_edits", "auto"] = "manual"

    @model_validator(mode="after")
    def ensure_disjoint(self) -> PermissionsConfig:
        overlap = set(self.always_allow) & set(self.always_deny)
        if overlap:
            raise ValueError(f"permission entries cannot be both allowed and denied: {sorted(overlap)}")
        return self


class SessionsConfig(StrictModel):
    directory: Path = Path(".lumen/sessions")


class ContextConfig(StrictModel):
    """Provider-independent context budgeting and compaction policy.

    ``ContextEngine`` resolves the active model profile, tokenizer, output
    reserve, and ratio-based soft/hard/target thresholds once, then uses that
    same policy for assembly, compaction, provider preflight, and reporting.
    ``soft_token_limit`` and ``keep_recent_tokens`` remain compatibility
    overrides; new configurations should normally keep the ratio defaults and
    put model-specific capability overrides under ``agent.models.*.context``.
    """

    enabled: bool = True
    soft_ratio: float = Field(default=0.80, gt=0.0, lt=1.0)
    hard_ratio: float = Field(default=0.92, gt=0.0, le=1.0)
    target_ratio: float = Field(default=0.55, gt=0.0, lt=1.0)
    soft_token_limit: int = Field(default=60_000, ge=1)
    #: Token budget for the recent window kept verbatim after compaction.
    #: Walked backwards from the newest message until the budget is spent,
    #: then snapped forward to a safe boundary (start of a user-prompt
    #: request). Replaces the older ``keep_recent_turns`` count-based cut.
    keep_recent_tokens: int = Field(default=20_000, ge=1)
    #: Per-tool-result character cap when serializing history for the
    #: summarizer. Each tool result is truncated to this many chars (head)
    #: before joining, so one giant output can't crowd out the rest —
    #: mirroring coding-agent's ``TOOL_RESULT_MAX_CHARS = 2000``.
    summary_tool_result_chars: int = Field(default=2_000, ge=100)
    summary_max_tokens: int = Field(default=2_000, ge=1)
    background_compaction: bool = True
    background_trigger_ratio: float = Field(default=0.90, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_threshold_ratios(self) -> ContextConfig:
        if not self.target_ratio < self.soft_ratio < self.hard_ratio:
            raise ValueError("context ratios must satisfy target_ratio < soft_ratio < hard_ratio")
        return self


class MemoryConfig(StrictModel):
    """Durable memory and opt-in background learning policy."""

    use: bool = True
    learn: bool = False
    external_context: Literal["exclude"] = "exclude"
    min_session_turns: int = Field(default=4, ge=1)
    idle_seconds: float = Field(default=120.0, ge=0.0)
    max_attempts: int = Field(default=5, ge=1)
    retry_base_seconds: float = Field(default=2.0, ge=0.0)


class UiConfig(StrictModel):
    """Terminal UI appearance preferences."""

    #: Active Textual theme at startup. Must be one of the builtin themes
    #: registered by :mod:`lumen.ui.themes`; switch live via ``/theme``.
    theme: str = "lumen-dark"
    transcript_density: Literal["normal", "verbose"] = "normal"
    animations: bool = True
    terminal_title: bool = True
    notifications: bool = True
    vim_mode: bool = False
    status_line: list[Literal["mode", "state", "model", "usage", "workspace", "keymap"]] = Field(
        default_factory=lambda: ["mode", "state", "model", "usage", "keymap"]
    )

    @field_validator("theme")
    @classmethod
    def validate_theme_name(cls, value: str) -> str:
        # Imported lazily so config loading does not pay for textual imports
        # in headless (--print / --check-config) runs.
        from lumen.ui.themes import BUILTIN_THEMES

        if value not in BUILTIN_THEMES:
            raise ValueError(f"ui.theme must be one of {sorted(BUILTIN_THEMES)}, got {value!r}")
        return value


class CollaborationConfig(StrictModel):
    default_mode: Literal["default", "plan"] = "default"


class SandboxConfig(StrictModel):
    mode: Literal["workspace_write", "disabled"] = "workspace_write"
    network: bool = False
    extra_read_paths: list[Path] = Field(default_factory=list[Path])
    extra_write_paths: list[Path] = Field(default_factory=list[Path])
    env_allow: list[str] = Field(default_factory=lambda: ["PATH", "LANG", "LC_ALL", "TERM", "TMPDIR"])


class WorkProductsConfig(StrictModel):
    enabled: bool = True
    auto_attach: bool = True
    strict: bool = True
    max_context_items: int = Field(default=8, ge=1, le=100)


class LiveTurnDetectionConfig(StrictModel):
    type: Literal["server_vad", "semantic_vad"] = "semantic_vad"
    eagerness: Literal["low", "medium", "high", "auto"] = "auto"
    interrupt_response: bool = True


class LiveTranscriptionConfig(StrictModel):
    enabled: bool = True
    model: str | None = "gpt-4o-mini-transcribe"
    language: str | None = "zh"


class LiveRouteBase(StrictModel):
    model: str
    api_key_env: str
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    voice: str


class OpenAILiveRouteConfig(LiveRouteBase):
    provider: Literal["openai"] = "openai"
    model: str = "gpt-realtime-2.1"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com"
    voice: str = "marin"
    completion_control: Literal["native_required_tool"] = "native_required_tool"


class BailianLiveRouteConfig(LiveRouteBase):
    provider: Literal["bailian"] = "bailian"
    model: str = "qwen3.5-omni-plus-realtime"
    api_key_env: str = "DASHSCOPE_API_KEY"
    workspace_id_env: str = "DASHSCOPE_WORKSPACE_ID"
    workspace_id: str | None = Field(default=None, exclude=True, repr=False)
    region: Literal["cn-beijing", "ap-southeast-1"] = "cn-beijing"
    base_url: str | None = None
    voice: str = "Tina"
    completion_control: Literal["host_gated_synthesis", "advisory_only"] = (
        "host_gated_synthesis"
    )


LiveRouteConfig = Annotated[
    OpenAILiveRouteConfig | BailianLiveRouteConfig,
    Field(discriminator="provider"),
]


class LiveConfig(StrictModel):
    """Optional browser Realtime voice transport and policy."""

    enabled: bool = False
    provider: Literal["openai"] = "openai"
    model: str = "gpt-realtime-2.1"
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    base_url: str = "https://api.openai.com"
    voice: str = "marin"
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    turn_detection: LiveTurnDetectionConfig = Field(default_factory=LiveTurnDetectionConfig)
    input_transcription: LiveTranscriptionConfig = Field(default_factory=LiveTranscriptionConfig)
    strict_completion: bool = True
    max_sessions: int = Field(default=1, ge=1, le=8)
    rollover_seconds: int = Field(default=3300, ge=60, le=3540)
    persist_audio: Literal[False] = False
    max_context_items: int = Field(default=24, ge=1, le=100)
    default_route: str | None = None
    fallback_routes: list[str] = Field(default_factory=list[str])
    routes: dict[str, LiveRouteConfig] = Field(default_factory=dict[str, LiveRouteConfig])

    @model_validator(mode="after")
    def validate_routes(self) -> LiveConfig:
        if not self.routes:
            if self.default_route is not None or self.fallback_routes:
                raise ValueError("live.default_route/fallback_routes require live.routes")
            return self
        if self.default_route is None:
            raise ValueError("live.default_route is required when live.routes are configured")
        if self.default_route not in self.routes:
            raise ValueError(f"live.default_route is not configured: {self.default_route}")
        missing = [name for name in self.fallback_routes if name not in self.routes]
        if missing:
            raise ValueError(f"live fallback routes are not configured: {', '.join(missing)}")
        return self


class AppConfig(StrictModel):
    version: Literal[2]
    agent: AgentSection
    ui: UiConfig = Field(default_factory=UiConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    collaboration: CollaborationConfig = Field(default_factory=CollaborationConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    work_products: WorkProductsConfig = Field(default_factory=WorkProductsConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    delegation: DelegationConfig = Field(default_factory=DelegationConfig)
    hooks: list[HookConfig] = Field(default_factory=list[HookConfig])
    config_path: Path = Field(exclude=True)
    config_sources: tuple[Any, ...] = Field(default_factory=tuple, exclude=True, repr=False)
    config_warnings: tuple[str, ...] = Field(default_factory=tuple, exclude=True, repr=False)
    project_trusted: bool = Field(default=True, exclude=True, repr=False)
    mcp_diagnostics: tuple[dict[str, Any], ...] = Field(default_factory=tuple, exclude=True, repr=False)


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_environment(value: str, *, variables: dict[str, str] | None = None) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if variables is not None and name in variables:
            resolved = variables[name]
            return default if default is not None and resolved == "" else resolved
        value = os.environ.get(name)
        if value is not None and (value != "" or default is None):
            return value
        if default is not None:
            return default
        raise ConfigLoadError(f"environment variable {name} is not set")

    return _ENV_PATTERN.sub(replace, value)


def validate_config_data(
    raw: dict[str, Any],
    *,
    config_path: Path,
    workspace: Path,
    config_sources: tuple[Any, ...] = (),
    config_warnings: tuple[str, ...] = (),
    project_trusted: bool = True,
) -> AppConfig:
    """Validate merged configuration and resolve runtime-only values.

    Layer-specific relative paths are made absolute by ``ConfigResolver``
    before this function is called. Relative paths that remain here belong to
    the primary/single configuration file, preserving ``load_config``'s public
    compatibility contract.
    """

    config_path = config_path.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    warnings = list(config_warnings)
    raw = dict(raw)
    if raw.get("version") == 1:
        # V1 is a strict subset of V2. Migrate it only in memory so existing
        # user configuration keeps working without silently rewriting a file
        # that may contain credentials or local comments.
        raw["version"] = 2
        raw_permissions = raw.get("permissions")
        if isinstance(raw_permissions, dict):
            legacy_permissions = cast(dict[str, Any], raw_permissions)
            if legacy_permissions.get("default_mode") == "ask":
                raw["permissions"] = {**legacy_permissions, "default_mode": "manual"}
        warnings.append("configuration version 1 was migrated in memory; update the file to version 2")
    raw_agents = raw.get("agents")
    raw_delegation = raw.get("delegation")
    if raw_agents is None and isinstance(raw_delegation, dict):
        legacy = cast(dict[str, Any], raw_delegation)
        raw["agents"] = {
            "enabled": bool(legacy.get("enabled", False)),
            "max_concurrency": legacy.get("max_concurrency", 3),
            "worktree_root": legacy.get("worktree_root", "~/.lumen/worktrees"),
            "request_count": legacy.get("request_count", 10),
            "tool_calls": legacy.get("tool_calls", 20),
            "timeout_seconds": legacy.get("timeout_seconds", 180.0),
        }
        warnings.append("delegation is deprecated; use agents")
    elif raw_agents is not None and raw_delegation is not None:
        warnings.append("both agents and delegation are configured; agents takes precedence")
    raw_context = raw.get("context")
    if isinstance(raw_context, dict):
        if "soft_token_limit" in raw_context:
            warnings.append(
                "context.soft_token_limit is deprecated; use context.soft_ratio or a "
                "model context.soft_limit_tokens override"
            )
        if "keep_recent_tokens" in raw_context:
            warnings.append(
                "context.keep_recent_tokens is deprecated as a global field; use a model "
                "context.keep_recent_tokens override"
            )
    try:
        config = AppConfig.model_validate(
            {
                **raw,
                "config_path": config_path,
                "config_sources": config_sources,
                "config_warnings": tuple(dict.fromkeys(warnings)),
                "project_trusted": project_trusted,
            }
        )
    except ValidationError as error:
        raise ConfigLoadError(str(error)) from error

    base = config_path.parent
    instructions_file = config.agent.instructions_file
    resolved_instructions = (
        (base / instructions_file).expanduser().resolve()
        if instructions_file is not None and not instructions_file.is_absolute()
        else instructions_file.expanduser().resolve()
        if instructions_file is not None
        else None
    )

    # Resolve API keys for every configured model. Both single- and multi-model
    # forms may declare api_key_env; we look the env var up once per model and
    # store the value on the (excluded) api_key field. Plaintext api_key values
    # declared directly in YAML are preserved verbatim.
    def resolve_model(model_cfg: ModelSettingsConfig) -> ModelSettingsConfig:
        if model_cfg.api_key_env:
            name = model_cfg.api_key_env
            value = os.environ.get(name)
            if value is None:
                raise ConfigLoadError(f"environment variable {name} is not set")
            return model_cfg.model_copy(update={"api_key": value})
        return model_cfg

    resolved_single = resolve_model(config.agent.model) if config.agent.model is not None else None
    resolved_models = {name: resolve_model(model_cfg) for name, model_cfg in config.agent.models.items()}
    resolved_live = config.live
    if config.live.enabled:
        if config.live.routes:
            resolved_routes: dict[str, LiveRouteConfig] = {}
            for route_name, route in config.live.routes.items():
                live_key = os.environ.get(route.api_key_env)
                if live_key is None:
                    raise ConfigLoadError(f"environment variable {route.api_key_env} is not set")
                updates: dict[str, Any] = {"api_key": live_key}
                if isinstance(route, BailianLiveRouteConfig):
                    workspace_id = os.environ.get(route.workspace_id_env)
                    if workspace_id is None:
                        raise ConfigLoadError(
                            f"environment variable {route.workspace_id_env} is not set"
                        )
                    updates["workspace_id"] = workspace_id
                resolved_routes[route_name] = route.model_copy(update=updates)
            resolved_live = config.live.model_copy(update={"routes": resolved_routes})
        else:
            live_key = os.environ.get(config.live.api_key_env)
            if live_key is None:
                raise ConfigLoadError(f"environment variable {config.live.api_key_env} is not set")
            resolved_live = config.live.model_copy(update={"api_key": live_key})

    resolved_servers: dict[str, McpServerConfig] = {}
    variables = dict(os.environ)
    # This built-in is intentionally assigned last and cannot be overridden by
    # the process environment or an MCP server's own env mapping.
    variables["LUMEN_PROJECT_DIR"] = str(workspace)
    for name, server in config.mcp_servers.items():
        resolved_env = {
            key: _expand_environment(value, variables=variables) for key, value in server.env.items()
        }
        if server.transport == "stdio":
            resolved_env["LUMEN_PROJECT_DIR"] = str(workspace)
        resolved_servers[name] = server.model_copy(
            update={
                "command": _expand_environment(server.command, variables=variables)
                if server.command is not None
                else None,
                "args": [_expand_environment(value, variables=variables) for value in server.args],
                "url": _expand_environment(server.url, variables=variables)
                if server.url is not None
                else None,
                "env": resolved_env,
                "headers": {
                    key: _expand_environment(value, variables=variables)
                    for key, value in server.headers.items()
                },
                "oauth": (
                    server.oauth.model_copy(
                        update={
                            "client_id": _expand_environment(server.oauth.client_id, variables=variables)
                            if server.oauth.client_id is not None
                            else None,
                            "client_secret": _expand_environment(
                                server.oauth.client_secret, variables=variables
                            )
                            if server.oauth.client_secret is not None
                            else None,
                        }
                    )
                    if server.oauth is not None
                    else None
                ),
            }
        )

    session_directory = config.sessions.directory.expanduser()
    if not session_directory.is_absolute():
        session_directory = base / session_directory

    return config.model_copy(
        update={
            "agent": config.agent.model_copy(
                update={
                    "instructions_file": resolved_instructions,
                    "model": resolved_single,
                    "models": resolved_models,
                }
            ),
            "tools": config.tools.model_copy(
                update={
                    "plugins": [
                        plugin.model_copy(
                            update={
                                "source_dir": (
                                    plugin.source_dir.expanduser().resolve()
                                    if plugin.source_dir is not None
                                    else base
                                )
                            }
                        )
                        for plugin in config.tools.plugins
                    ]
                }
            ),
            "mcp_servers": resolved_servers,
            "agents": config.agents.model_copy(
                update={"worktree_root": config.agents.worktree_root.expanduser().resolve()}
            ),
            "live": resolved_live,
            "sessions": config.sessions.model_copy(update={"directory": session_directory.resolve()}),
        }
    )


def load_config(path: str | Path, *, workspace: str | Path | None = None) -> AppConfig:
    """Load one explicit configuration file.

    This remains the compatibility API for callers that intentionally select a
    single file. CLI default discovery lives in :mod:`lumen.config_resolver`.
    """

    config_path = Path(path).expanduser().resolve()
    try:
        loaded: object = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigLoadError(f"cannot read configuration {config_path}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigLoadError(f"invalid YAML in {config_path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ConfigLoadError("configuration root must be a mapping")
    raw = cast(dict[str, Any], loaded)

    effective_workspace = (
        Path(workspace).expanduser().resolve() if workspace is not None else config_path.parent
    )
    raw_copy: dict[str, Any] = dict(raw)
    sessions_value = raw_copy.get("sessions")
    sessions = cast(dict[str, Any], sessions_value) if isinstance(sessions_value, dict) else {}
    if "directory" not in sessions:
        raw_copy["sessions"] = {
            **sessions,
            "directory": effective_workspace / ".lumen" / "sessions",
        }
    return validate_config_data(
        raw_copy,
        config_path=config_path,
        workspace=effective_workspace,
    )
