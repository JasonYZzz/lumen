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
        """version: 1
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


def test_higher_single_model_clears_inherited_models(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    _write(
        home / ".lumen" / "agent.yaml",
        """version: 1
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

    config = ConfigResolver(workspace, home=home).resolve(project_trusted=True).config

    assert config.agent.model is not None
    assert config.agent.models == {}
    assert config.agent.default_model is None


def test_explicit_file_is_exclusive_and_cli_wins_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    environment_config = tmp_path / "environment.yaml"
    explicit_config = tmp_path / "explicit.yaml"
    _write(environment_config, "version: 1\nagent: {name: env, model: {id: test}}\n")
    _write(explicit_config, "version: 1\nagent: {name: cli, model: {id: test}}\n")

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
        """version: 1
agent:
  model: {id: test}
  instructions_file: prompts/system.md
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

    assert config.agent.instructions_file == home / ".lumen" / "prompts" / "system.md"
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
