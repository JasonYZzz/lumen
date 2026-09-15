from pathlib import Path

import pytest

from lumen.config import AppConfig, ConfigLoadError, load_config
from lumen.context import resolve_context_policy


def test_run_limits_are_opt_in_and_finite_legacy_values_remain_valid() -> None:
    from pydantic import ValidationError

    from lumen.config import AgentsConfig, LimitsConfig

    defaults = LimitsConfig()
    assert defaults.request_count is None
    assert defaults.tool_calls is None
    assert defaults.model_request_timeout_seconds is None
    assert defaults.model_stream_idle_timeout_seconds == 300
    assert defaults.model_retries == 5
    assert defaults.parallel_tool_calls == "parallel_safe"
    assert LimitsConfig(parallel_tool_calls="sequential").parallel_tool_calls == "sequential"
    finite = LimitsConfig(request_count=60, tool_calls=100, model_request_timeout_seconds=300)
    assert (finite.request_count, finite.tool_calls, finite.model_request_timeout_seconds) == (60, 100, 300)
    assert LimitsConfig(tool_calls=0).tool_calls == 0
    assert AgentsConfig().timeout_seconds is None
    with pytest.raises(ValidationError):
        LimitsConfig(request_count=0)
    with pytest.raises(ValidationError):
        LimitsConfig(model_stream_idle_timeout_seconds=0)


def write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_resolves_relative_paths_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_TOKEN", "secret-model-token")
    monkeypatch.setenv("MCP_TOKEN", "secret-mcp-token")
    config_path = write_config(
        tmp_path / "agent.yaml",
        """
version: 2
agent:
  name: test-agent
  prompt:
    mode: append
    append_file: prompts/system.md
  model:
    id: openai:gpt-5
    api_key_env: MODEL_TOKEN
  limits: {}
tools:
  builtins: [read_file]
mcp_servers:
  weather:
    transport: streamable_http
    url: https://example.test/mcp
    headers:
      Authorization: Bearer ${MCP_TOKEN}
sessions:
  directory: state/sessions
""",
    )

    config = load_config(config_path)

    assert config.agent.prompt.append_file == (tmp_path / "prompts/system.md").resolve()
    assert config.agent.model is not None
    assert config.agent.model.api_key == "secret-model-token"
    assert config.mcp_servers["weather"].headers == {"Authorization": "Bearer secret-mcp-token"}
    assert config.sessions.directory == (tmp_path / "state/sessions").resolve()


def test_load_config_rejects_removed_instructions_file(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "agent.yaml",
        """version: 2
agent:
  model: {id: test}
  instructions_file: prompts/system.md
""",
    )

    with pytest.raises(ConfigLoadError, match="instructions_file"):
        load_config(config_path)


def test_load_config_rejects_missing_environment_variable(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "agent.yaml",
        """
version: 2
agent:
  model:
    id: openai:gpt-5
    api_key_env: MISSING_MODEL_TOKEN
""",
    )

    with pytest.raises(ConfigLoadError, match="MISSING_MODEL_TOKEN"):
        load_config(config_path)


def test_load_config_rejects_unknown_fields(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "agent.yaml",
        """
version: 2
agent:
  model:
    id: test
  surprise: true
""",
    )

    with pytest.raises(ConfigLoadError, match="surprise"):
        load_config(config_path)


def test_mcp_transport_fields_are_mutually_exclusive(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "agent.yaml",
        """
version: 2
agent:
  model:
    id: test
mcp_servers:
  broken:
    transport: stdio
    command: python
    url: https://example.test/mcp
""",
    )

    with pytest.raises(ConfigLoadError, match="url"):
        load_config(config_path)


def load_yaml(tmp_path: Path, text: str) -> AppConfig:
    path = tmp_path / "agent.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_context_config_defaults_apply(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent: {model: {id: test}}
""",
    )
    assert config.context.enabled is True
    assert config.context.soft_token_limit == 60_000
    assert config.context.keep_recent_tokens == 20_000
    assert config.context.summary_tool_result_chars == 2_000
    assert config.context.summary_max_tokens == 2_000
    assert config.agent.limits.output_limit_retries == 3
    assert config.work_products.enabled is True
    assert config.work_products.auto_attach is True
    assert config.work_products.strict is True
    assert config.work_products.max_context_items == 8


def test_work_products_and_mcp_effects_are_configurable(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent: {model: {id: test}}
work_products:
  enabled: true
  auto_attach: false
  strict: false
  max_context_items: 12
mcp_servers:
  records:
    transport: stdio
    command: records-server
    tool_effects:
      lookup: observe
      update: mutation
""",
    )

    assert config.work_products.max_context_items == 12
    assert config.work_products.auto_attach is False
    assert config.mcp_servers["records"].tool_effects == {
        "lookup": "observe",
        "update": "mutation",
    }


def test_live_routes_resolve_provider_specific_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "ws-cn-1")
    config = load_yaml(
        tmp_path,
        """
version: 2
agent: {model: {id: test}}
live:
  enabled: true
  default_route: cn-primary
  fallback_routes: [cn-fast]
  routes:
    cn-primary:
      provider: bailian
      model: qwen3.5-omni-plus-realtime
      region: cn-beijing
      api_key_env: DASHSCOPE_API_KEY
      workspace_id_env: DASHSCOPE_WORKSPACE_ID
      voice: Tina
      completion_control: host_gated_synthesis
    cn-fast:
      provider: bailian
      model: qwen3.5-omni-flash-realtime
      region: cn-beijing
      api_key_env: DASHSCOPE_API_KEY
      workspace_id_env: DASHSCOPE_WORKSPACE_ID
      voice: Cherry
      completion_control: advisory_only
  strict_completion: true
""",
    )

    primary = config.live.routes["cn-primary"]
    assert primary.provider == "bailian"
    assert primary.api_key == "dashscope-secret"
    assert primary.workspace_id == "ws-cn-1"
    assert config.live.default_route == "cn-primary"
    assert config.live.fallback_routes == ["cn-fast"]


def test_live_routes_reject_an_unknown_default_route(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError):
        load_yaml(
            tmp_path,
            """
version: 2
agent: {model: {id: test}}
live:
  default_route: missing
  routes:
    primary:
      provider: openai
      model: gpt-realtime
""",
        )


def test_context_config_is_strict_and_validated(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent: {model: {id: test}}
context:
  enabled: true
  soft_token_limit: 20000
  keep_recent_tokens: 12000
  summary_max_tokens: 1500
""",
    )
    assert config.context.soft_token_limit == 20_000
    assert config.context.keep_recent_tokens == 12_000
    assert config.context.summary_max_tokens == 1_500


def test_context_config_rejects_non_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError):
        load_yaml(
            tmp_path,
            """
version: 2
agent: {model: {id: test}}
context:
  soft_token_limit: 0
""",
        )


def test_capability_builtin_names_are_accepted(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent: {model: {id: test}}
tools:
  builtins:
    - read_file
    - write_file
    - edit_file
    - run_command
    - git_status
    - git_diff
    - git_stage
    - git_commit
    - git_push
""",
    )
    assert config.tools.builtins[-5:] == [
        "git_status", "git_diff", "git_stage", "git_commit", "git_push",
    ]


# ---------------------------------------------------------------------------
# Multi-model configuration
# ---------------------------------------------------------------------------


def test_multi_model_form_loads_registry_and_default(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent:
  default_model: glm-5.2
  models:
    deepseek-flash:
      id: openai:deepseek-flash
      api_key: sk-deepseek
      base_url: https://api.deepseek.com/v1
    glm-5.2:
      id: openai:glm-5.2
      api_key: sk-dashscope
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
""",
    )
    assert config.agent.model is None
    assert set(config.agent.models) == {"deepseek-flash", "glm-5.2"}
    assert config.agent.default_model == "glm-5.2"
    assert config.agent.default_model_name() == "glm-5.2"
    assert config.agent.model_registry()["glm-5.2"].api_key == "sk-dashscope"


def test_multi_model_defaults_to_first_key_when_default_unset(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent:
  models:
    alpha: {id: openai:alpha, api_key: k1}
    beta: {id: openai:beta, api_key: k2}
""",
    )
    assert config.agent.default_model is None
    # Pydantic preserves insertion order for dicts; alpha is first.
    assert config.agent.default_model_name() == "alpha"


def test_native_web_search_defaults_to_auto_and_rejects_openai_chat_force() -> None:
    from pydantic import ValidationError

    from lumen.config import ModelSettingsConfig
    from lumen.models import native_web_search_enabled

    deepseek = ModelSettingsConfig(
        id="openai:deepseek-flash",
        base_url="https://api.deepseek.com",
        api="responses",
    )
    assert deepseek.native_web_search.mode == "auto"
    assert native_web_search_enabled(deepseek) is False
    assert native_web_search_enabled(
        ModelSettingsConfig(
            id="openai:k3",
            base_url="https://api.kimi.com/coding/v1",
            api="responses",
        )
    ) is True
    for model in ("qwen3.8-max", "qwen3.8-flash"):
        assert native_web_search_enabled(
            ModelSettingsConfig(
                id=f"openai:{model}",
                api="responses",
                base_url="https://workspace.cn-beijing.maas.aliyuncs.com/api/v2/apps/protocols/compatible-mode/v1",
            )
        ) is True
    assert native_web_search_enabled(ModelSettingsConfig(id="openai:unknown-model")) is False
    assert native_web_search_enabled(
        ModelSettingsConfig.model_validate(
            {"id": "openai:unknown-model", "native_web_search": {"mode": "enabled"}}
        )
    ) is True
    with pytest.raises(ValidationError, match="requires the Responses API"):
        ModelSettingsConfig.model_validate(
            {
                "id": "openai:legacy-model",
                "api": "chat",
                "native_web_search": {"mode": "enabled"},
            }
        )


def test_web_search_config_validates_provider_specific_fields() -> None:
    from pydantic import ValidationError

    from lumen.config import WebSearchConfig

    searxng = WebSearchConfig(provider="searxng", base_url="http://127.0.0.1:8080")
    assert searxng.api_key_env is None
    # searxng requires base_url.
    with pytest.raises(ValidationError, match="base_url"):
        WebSearchConfig(provider="searxng")
    # tavily/brave still require api_key_env.
    with pytest.raises(ValidationError, match="api_key_env"):
        WebSearchConfig(provider="tavily")
    with pytest.raises(ValidationError, match="api_key_env"):
        WebSearchConfig(provider="brave")
    # searxng-only fields are rejected for commercial providers.
    with pytest.raises(ValidationError, match="base_url"):
        WebSearchConfig(provider="brave", api_key_env="BRAVE_KEY", base_url="http://x")
    with pytest.raises(ValidationError, match="engines"):
        WebSearchConfig(provider="tavily", api_key_env="TAVILY_KEY", engines=["duckduckgo"])
    with pytest.raises(ValidationError, match="language"):
        WebSearchConfig(provider="brave", api_key_env="BRAVE_KEY", language="all")
    # An explicit but empty engines list is rejected.
    with pytest.raises(ValidationError, match="engines"):
        WebSearchConfig(provider="searxng", base_url="http://127.0.0.1:8080", engines=[])


def test_web_tools_fetch_strategy_defaults_to_auto_and_validates() -> None:
    from pydantic import ValidationError

    from lumen.config import WebToolsConfig

    assert WebToolsConfig().fetch_strategy == "auto"
    assert WebToolsConfig(fetch_strategy="http_only").fetch_strategy == "http_only"
    with pytest.raises(ValidationError):
        WebToolsConfig(fetch_strategy="browser_first")  # type: ignore[arg-type]


def test_mcp_activation_policy_rejects_unknown_server(tmp_path: Path) -> None:
    enabled = load_yaml(
        tmp_path,
        """
version: 2
agent: {model: {id: test}}
mcp_servers:
  exa: {transport: streamable_http, url: https://mcp.exa.ai/mcp}
mcp:
  enabled: {exa: false}
""",
    )
    assert enabled.mcp.enabled == {"exa": False}
    with pytest.raises(ConfigLoadError, match="unknown servers"):
        load_yaml(
            tmp_path,
            """
version: 2
agent: {model: {id: test}}
mcp:
  enabled: {missing: false}
""",
        )


def test_single_model_form_still_supported(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent:
  model: {id: openai:gpt-5, api_key: k}
""",
    )
    assert config.agent.model is not None
    assert config.agent.models == {}
    registry = config.agent.model_registry()
    # The single-model form synthesises a name from the id minus provider.
    assert registry == {"gpt-5": config.agent.model}
    assert config.agent.default_model_name() == "gpt-5"


def test_model_and_models_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="not both"):
        load_yaml(
            tmp_path,
            """
version: 2
agent:
  model: {id: openai:gpt-5}
  models:
    other: {id: openai:other}
""",
        )


def test_neither_model_nor_models_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="must define"):
        load_yaml(
            tmp_path,
            """
version: 2
agent:
  name: no-model
""",
        )


def test_default_model_must_be_in_models(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="default_model"):
        load_yaml(
            tmp_path,
            """
version: 2
agent:
  default_model: missing
  models:
    real: {id: openai:real}
""",
        )


def test_default_model_rejected_with_single_model_form(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="default_model requires"):
        load_yaml(
            tmp_path,
            """
version: 2
agent:
  default_model: x
  model: {id: openai:x}
""",
        )


def test_multi_model_resolves_api_key_env_per_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "resolved-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "resolved-dashscope")
    config = load_yaml(
        tmp_path,
        """
version: 2
agent:
  models:
    deepseek-flash:
      id: openai:deepseek-flash
      api_key_env: DEEPSEEK_API_KEY
      base_url: https://api.deepseek.com/v1
    glm-5.2:
      id: openai:glm-5.2
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
""",
    )
    assert config.agent.models["deepseek-flash"].api_key == "resolved-deepseek"
    assert config.agent.models["glm-5.2"].api_key == "resolved-dashscope"


def test_multi_model_missing_env_var_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="DEEPSEEK_API_KEY"):
        load_yaml(
            tmp_path,
            """
version: 2
agent:
  models:
    deepseek-flash:
      id: openai:deepseek-flash
      api_key_env: DEEPSEEK_API_KEY
""",
        )


def test_multi_model_plaintext_api_key_preserved(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 2
agent:
  models:
    local:
      id: openai:local
      api_key: sk-plaintext
""",
    )
    assert config.agent.models["local"].api_key == "sk-plaintext"


def test_repo_example_config_loads_without_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """The committed agent.example.yaml must always load cleanly against the
    current config schema — guards against field renames/removals shipping in
    a config that the schema then rejects (e.g. keep_recent_turns →
    keep_recent_tokens, read_only_tools → tool_risks)."""
    import lumen

    repo_root = Path(lumen.__file__).resolve().parents[2]
    example = repo_root / "agent.example.yaml"
    assert example.is_file(), f"agent.example.yaml not found at {example}"
    # The example references real provider env vars; stub them so schema
    # validation is what's under test, not key availability.
    for name in (
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "KIMI_API_KEY",
        "OPENAI_API_KEY",
        "OMLX_API_KEY",
        "TYC_TOKEN",
        "EXA_API_KEY",
    ):
        monkeypatch.setenv(name, "stub")
    config = load_config(example)
    assert config.version == 2
    # Prove that the example's real model fields survive YAML validation and
    # feed the same resolved policy used by ContextEngine at startup.
    flash = config.agent.models["deepseek-flash"]
    assert flash.context.profile == "deepseek-flash"
    assert flash.settings["max_tokens"] == 65_536
    policy = resolve_context_policy(flash, config.context)
    assert policy.profile_id == "deepseek-flash"
    assert policy.context_window_tokens == 1_000_000
    assert policy.architectural_max_output_tokens == 384_000
    assert policy.output_reserve_tokens == 65_536
    assert config.agent.models["kimi-k3"].input_modalities == ("text", "image")
    # The example demonstrates tool_risks on optional remote servers.
    tyc = config.mcp_servers["tyc-mcp"]
    assert tyc.tool_risks.get("search_companies") == "read"
    assert tyc.tool_risks.get("call_tool") == "execute"
    exa = config.mcp_servers["exa"]
    assert exa.tool_risks == {"web_fetch_exa": "read", "web_search_exa": "read"}
    assert all(not server.read_only_tools for server in config.mcp_servers.values())


@pytest.mark.parametrize("legacy_mode", ["plan", "ask"])
def test_config_v2_rejects_legacy_approval_modes(tmp_path: Path, legacy_mode: str) -> None:
    path = write_config(
        tmp_path / "agent.yaml",
        f"""
version: 2
agent: {{model: {{id: test}}}}
permissions: {{default_mode: {legacy_mode}}}
""",
    )

    with pytest.raises(ConfigLoadError, match="default_mode"):
        load_config(path)


def test_config_migrates_v1_in_memory_without_rewriting_source(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "agent.yaml",
        """
version: 1
agent: {model: {id: test}}
permissions: {default_mode: ask}
""",
    )
    original = path.read_text(encoding="utf-8")

    config = load_config(path)

    assert config.version == 2
    assert config.permissions.default_mode == "manual"
    assert "version 1" in " ".join(config.config_warnings)
    assert path.read_text(encoding="utf-8") == original
