from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigLoadError(ValueError):
    """Raised when an agent configuration cannot be loaded safely."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class ToolsConfig(StrictModel):
    builtins: list[
        Literal[
            "read_file",
            "list_directory",
            "search_text",
            "write_file",
            "edit_file",
            "run_command",
        ]
    ] = Field(default_factory=lambda: ["read_file", "list_directory", "search_text"])
    plugins: list[PluginConfig] = Field(default_factory=list[PluginConfig])


class McpServerConfig(StrictModel):
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
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
    required: bool = True

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
        return self


class PermissionsConfig(StrictModel):
    always_allow: list[str] = Field(default_factory=list)
    always_deny: list[str] = Field(default_factory=list)
    #: Session approval mode. ``manual`` confirms every tool that the permission
    #: policy routes to CONFIRM; ``accept_edits`` only auto-approves builtin
    #: write/edit tools; ``auto`` approves every explicitly classified risk.
    #: Undeclared remote (MCP) tools default to ``external_unknown`` and always
    #: prompt. Toggle live via ``/mode``, ``Ctrl+M``, or ``Shift+Tab`` without
    #: rebuilding the runtime.
    default_mode: Literal["manual", "accept_edits", "auto"] = "manual"

    @field_validator("default_mode", mode="before")
    @classmethod
    def normalize_legacy_mode(cls, value: object) -> object:
        return "manual" if value == "ask" else value

    @model_validator(mode="after")
    def ensure_disjoint(self) -> PermissionsConfig:
        overlap = set(self.always_allow) & set(self.always_deny)
        if overlap:
            raise ValueError(f"permission entries cannot be both allowed and denied: {sorted(overlap)}")
        return self


class SessionsConfig(StrictModel):
    directory: Path = Path(".lumen/sessions")


class ContextConfig(StrictModel):
    """Provider-independent context compaction policy.

    Compaction runs once per ``run`` when the estimated active history exceeds
    ``soft_token_limit``. The active model context is rebuilt from a structured
    summary plus a token-budgeted recent window while the JSONL session
    repository still preserves the full raw history.

    The recent-window size is controlled by ``keep_recent_tokens`` (default
    20000) — a backward-walking token accumulator snaps to a safe boundary so
    a tool result is never orphaned from its call. This mirrors coding-agent's
    ``findCutPoint`` and replaces the older fixed-turn-count approach, which
    was unpredictable (6 turns could be 2K or 60K tokens depending on size).
    """

    enabled: bool = True
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


class AppConfig(StrictModel):
    version: Literal[1]
    agent: AgentSection
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    config_path: Path = Field(exclude=True)


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_environment(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError as error:
            raise ConfigLoadError(f"environment variable {name} is not set") from error

    return _ENV_PATTERN.sub(replace, value)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigLoadError(f"cannot read configuration {config_path}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigLoadError(f"invalid YAML in {config_path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigLoadError("configuration root must be a mapping")

    try:
        config = AppConfig.model_validate({**raw, "config_path": config_path})
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

    resolved_servers: dict[str, McpServerConfig] = {}
    for name, server in config.mcp_servers.items():
        resolved_servers[name] = server.model_copy(
            update={
                "env": {key: _expand_environment(value) for key, value in server.env.items()},
                "headers": {key: _expand_environment(value) for key, value in server.headers.items()},
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
            "mcp_servers": resolved_servers,
            "sessions": config.sessions.model_copy(update={"directory": session_directory.resolve()}),
        }
    )
