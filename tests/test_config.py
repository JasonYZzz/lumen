from pathlib import Path

import pytest

from lumen.config import AppConfig, ConfigLoadError, load_config
from lumen.context import resolve_context_policy


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
version: 1
agent:
  name: test-agent
  instructions_file: prompts/system.md
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

    assert config.agent.instructions_file == (tmp_path / "prompts/system.md").resolve()
    assert config.agent.model is not None
    assert config.agent.model.api_key == "secret-model-token"
    assert config.mcp_servers["weather"].headers == {"Authorization": "Bearer secret-mcp-token"}
    assert config.sessions.directory == (tmp_path / "state/sessions").resolve()


def test_load_config_rejects_missing_environment_variable(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "agent.yaml",
        """
version: 1
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
version: 1
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
version: 1
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
version: 1
agent: {model: {id: test}}
""",
    )
    assert config.context.enabled is True
    assert config.context.soft_token_limit == 60_000
    assert config.context.keep_recent_tokens == 20_000
    assert config.context.summary_tool_result_chars == 2_000
    assert config.context.summary_max_tokens == 2_000


def test_context_config_is_strict_and_validated(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 1
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
version: 1
agent: {model: {id: test}}
context:
  soft_token_limit: 0
""",
        )


def test_capability_builtin_names_are_accepted(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 1
agent: {model: {id: test}}
tools:
  builtins: [read_file, write_file, edit_file, run_command]
""",
    )
    assert config.tools.builtins[-3:] == ["write_file", "edit_file", "run_command"]


# ---------------------------------------------------------------------------
# Multi-model configuration
# ---------------------------------------------------------------------------


def test_multi_model_form_loads_registry_and_default(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 1
agent:
  default_model: glm-5.2
  models:
    deepseek-v4-pro:
      id: openai:deepseek-v4-pro
      api_key: sk-deepseek
      base_url: https://api.deepseek.com/v1
    glm-5.2:
      id: openai:glm-5.2
      api_key: sk-dashscope
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
""",
    )
    assert config.agent.model is None
    assert set(config.agent.models) == {"deepseek-v4-pro", "glm-5.2"}
    assert config.agent.default_model == "glm-5.2"
    assert config.agent.default_model_name() == "glm-5.2"
    assert config.agent.model_registry()["glm-5.2"].api_key == "sk-dashscope"


def test_multi_model_defaults_to_first_key_when_default_unset(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 1
agent:
  models:
    alpha: {id: openai:alpha, api_key: k1}
    beta: {id: openai:beta, api_key: k2}
""",
    )
    assert config.agent.default_model is None
    # Pydantic preserves insertion order for dicts; alpha is first.
    assert config.agent.default_model_name() == "alpha"


def test_single_model_form_still_supported(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 1
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
version: 1
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
version: 1
agent:
  name: no-model
""",
        )


def test_default_model_must_be_in_models(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="default_model"):
        load_yaml(
            tmp_path,
            """
version: 1
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
version: 1
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
version: 1
agent:
  models:
    deepseek-v4-pro:
      id: openai:deepseek-v4-pro
      api_key_env: DEEPSEEK_API_KEY
      base_url: https://api.deepseek.com/v1
    glm-5.2:
      id: openai:glm-5.2
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
""",
    )
    assert config.agent.models["deepseek-v4-pro"].api_key == "resolved-deepseek"
    assert config.agent.models["glm-5.2"].api_key == "resolved-dashscope"


def test_multi_model_missing_env_var_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="DEEPSEEK_API_KEY"):
        load_yaml(
            tmp_path,
            """
version: 1
agent:
  models:
    deepseek-v4-pro:
      id: openai:deepseek-v4-pro
      api_key_env: DEEPSEEK_API_KEY
""",
        )


def test_multi_model_plaintext_api_key_preserved(tmp_path: Path) -> None:
    config = load_yaml(
        tmp_path,
        """
version: 1
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
        "TYC_TOKEN",
        "EXA_API_KEY",
    ):
        monkeypatch.setenv(name, "stub")
    config = load_config(example)
    assert config.version == 1
    # Prove that the example's real model fields survive YAML validation and
    # feed the same resolved policy used by ContextEngine at startup.
    flash = config.agent.models["deepseek-v4-flash"]
    assert flash.context.profile == "deepseek-v4-flash"
    assert flash.settings["max_tokens"] == 65_536
    policy = resolve_context_policy(flash, config.context)
    assert policy.profile_id == "deepseek-v4-flash"
    assert policy.context_window_tokens == 1_000_000
    assert policy.architectural_max_output_tokens == 384_000
    assert policy.output_reserve_tokens == 65_536
    # The example demonstrates tool_risks on optional remote servers.
    tyc = config.mcp_servers["tyc-mcp"]
    assert tyc.tool_risks.get("search_companies") == "read"
    assert tyc.tool_risks.get("call_tool") == "execute"
    exa = config.mcp_servers["exa"]
    assert exa.tool_risks == {"web_fetch_exa": "read", "web_search_exa": "read"}
    assert all(not server.read_only_tools for server in config.mcp_servers.values())
