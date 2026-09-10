from __future__ import annotations

from pathlib import Path

import pytest

from lumen.config import ConfigLoadError
from lumen.config_resolver import ConfigResolver, ConfigScope


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_four_layers_merge_with_special_rules(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write(
        home / ".lumen" / "agent.yaml",
        """version: 2
agent:
  name: user
  models:
    primary: {id: test}
    inherited: {id: test}
  default_model: primary
tools:
  builtins: [read_file, list_directory]
  plugins:
    - {module: shared, factory: create_tools}
mcp_servers:
  inherited:
    transport: stdio
    command: user-command
  shared:
    transport: stdio
    command: lower-command
permissions:
  always_allow: [read_file, run_command]
""",
    )
    _write(
        workspace / "agent.yaml",
        """agent:
  name: legacy
permissions:
  always_deny: [run_command]
""",
    )
    _write(
        workspace / ".lumen" / "agent.yaml",
        """mcp_servers:
  shared:
    transport: stdio
    command: project-command
tools:
  plugins:
    - {module: shared, factory: create_tools}
    - {module: project_only}
""",
    )
    _write(
        workspace / ".lumen" / "agent.local.yaml",
        """agent:
  name: local
tools:
  builtins: [search_text]
mcp_servers:
  inherited: null
""",
    )

    result = ConfigResolver(workspace, home=home).resolve(project_trusted=True)

    assert [source.scope for source in result.sources] == [
        ConfigScope.USER,
        ConfigScope.LEGACY,
        ConfigScope.PROJECT,
        ConfigScope.LOCAL,
    ]
    assert result.config.agent.name == "local"
    assert set(result.config.agent.models) == {"primary", "inherited"}
    assert result.config.tools.builtins == ["search_text"]
    assert [(item.module, item.source_dir) for item in result.config.tools.plugins] == [
        ("shared", workspace / ".lumen"),
        ("project_only", workspace / ".lumen"),
    ]
    assert set(result.config.mcp_servers) == {"shared"}
    assert result.config.mcp_servers["shared"].command == "project-command"
    assert result.config.mcp_servers["shared"].source_scope == "project"
    assert result.config.permissions.always_allow == ["read_file"]
    assert result.config.permissions.always_deny == ["run_command"]
    assert result.config.sessions.directory == workspace / ".lumen" / "sessions"
    assert result.warnings and "Legacy configuration" in result.warnings[0]


def test_resolution_report_redacts_runtime_secrets_and_tracks_winning_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    user_config = home / ".lumen" / "agent.yaml"
    project_config = workspace / ".lumen" / "agent.yaml"
    _write(
        user_config,
        """version: 2
agent:
  name: user-name
  model:
    id: test
    api_key_env: MODEL_TOKEN
    context: {window_tokens: 32000, tokenizer: {kind: conservative}}
mcp_servers:
  docs:
    transport: stdio
    command: python
    args: ["--token=${MCP_TOKEN}"]
    env: {ACCESS_TOKEN: "${MCP_TOKEN}"}
""",
    )
    _write(project_config, "agent: {name: project-name}\n")
    monkeypatch.setenv("MODEL_TOKEN", "model-secret-value")
    monkeypatch.setenv("MCP_TOKEN", "mcp-secret-value")

    report = ConfigResolver(workspace, home=home).resolve().report().as_dict()
    encoded = str(report)

    assert "model-secret-value" not in encoded
    assert "mcp-secret-value" not in encoded
    assert report["effective_config"]["agent"]["model"]["api_key_env"] == "MODEL_TOKEN"
    assert report["effective_config"]["agent"]["model"]["context"]["window_tokens"] == 32000
    assert report["effective_config"]["agent"]["model"]["context"]["tokenizer"] == {
        "kind": "conservative",
        "encoding": None,
    }
    server_env = report["effective_config"]["mcp_servers"]["docs"]["env"]
    assert server_env["ACCESS_TOKEN"] == "<redacted>"
    assert set(server_env.values()) == {"<redacted>"}
    assert report["effective_config"]["mcp_servers"]["docs"]["args"] == ["<redacted>"]
    assert report["provenance"]["agent.name"] == {
        "scope": "project",
        "path": str(project_config),
    }


def test_higher_single_model_clears_inherited_models(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write(
        home / ".lumen" / "agent.yaml",
        """version: 2
agent:
  models:
    first: {id: test}
  default_model: first
""",
    )
    _write(
        workspace / ".lumen" / "agent.local.yaml",
        """agent:
  model: {id: test}
""",
    )

    result = ConfigResolver(workspace, home=home).resolve(project_trusted=True)
    config = result.config

    assert config.agent.model is not None
    assert config.agent.models == {}
    assert config.agent.default_model is None
    provenance = result.report().as_dict()["provenance"]
    assert not any(field.startswith("agent.models") for field in provenance)
    assert provenance["agent.model.id"]["scope"] == "local"


def test_managed_source_override_participates_in_validation_and_provenance(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write(
        workspace / ".lumen" / "agent.yaml",
        """version: 2
agent:
  models:
    first: {id: test}
  default_model: first
""",
    )
    managed = workspace / ".lumen" / "agent.web.yaml"

    result = ConfigResolver(workspace, home=home).resolve(
        source_overrides={
            managed: {
                "version": 2,
                "agent": {
                    "models": {"second": {"id": "test"}},
                    "default_model": "second",
                },
            }
        }
    )

    assert result.sources[-1].scope is ConfigScope.MANAGED
    assert set(result.config.agent.models) == {"first", "second"}
    assert result.config.agent.default_model == "second"
    assert result.report().as_dict()["provenance"]["agent.models.second.id"] == {
        "scope": "managed",
        "path": str(managed),
    }


def test_explicit_file_is_exclusive_and_cli_wins_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    environment_config = tmp_path / "environment.yaml"
    explicit_config = tmp_path / "explicit.yaml"
    _write(environment_config, "version: 2\nagent: {name: env, model: {id: test}}\n")
    _write(explicit_config, "version: 2\nagent: {name: cli, model: {id: test}}\n")

    resolver = ConfigResolver(
        workspace,
        explicit_path=explicit_config,
        environ={"LUMEN_CONFIG": str(environment_config)},
    )
    result = resolver.resolve()

    assert result.exclusive is True
    assert result.config.agent.name == "cli"
    assert result.sources == (result.sources[0],)
    assert result.sources[0].scope is ConfigScope.EXPLICIT


def test_relative_paths_and_mcp_environment_follow_declaring_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    _write(
        home / ".lumen" / "agent.yaml",
        """version: 2
agent:
  model: {id: test}
  prompt:
    mode: append
    append_file: prompts/system.md
sessions:
  directory: state/sessions
mcp_servers:
  local:
    transport: stdio
    command: "${MISSING_COMMAND:-python}"
    args: ["${LUMEN_PROJECT_DIR}", "${OPTIONAL:-fallback}"]
    env:
      PROJECT: "${LUMEN_PROJECT_DIR}"
      LUMEN_PROJECT_DIR: cannot-override
""",
    )
    monkeypatch.delenv("MISSING_COMMAND", raising=False)
    monkeypatch.delenv("OPTIONAL", raising=False)

    config = ConfigResolver(workspace, home=home).resolve().config
    server = config.mcp_servers["local"]

    assert config.agent.prompt.append_file == home / ".lumen" / "prompts" / "system.md"
    assert config.sessions.directory == home / ".lumen" / "state" / "sessions"
    assert server.command == "python"
    assert server.args == [str(workspace), "fallback"]
    assert server.env["PROJECT"] == str(workspace)
    assert server.env["LUMEN_PROJECT_DIR"] == str(workspace)


def test_untrusted_project_file_is_not_parsed(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write(workspace / ".lumen" / "agent.yaml", "this: [is: invalid")

    resolver = ConfigResolver(workspace, home=tmp_path / "home")

    with pytest.raises(ConfigLoadError, match="project is not trusted"):
        resolver.resolve(project_trusted=False)


def test_missing_configuration_lists_search_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    with pytest.raises(ConfigLoadError, match="lumen init --global") as captured:
        ConfigResolver(workspace, home=tmp_path / "home").resolve()

    assert str(workspace / ".lumen" / "agent.yaml") in str(captured.value)
