import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch
from typer.testing import CliRunner

import lumen.cli as cli
from lumen.cli import app
from lumen.config import AppConfig

# This module intentionally verifies the private version fallback seam.
# pyright: reportPrivateUsage=false


def _minimal_config(name: str = "global") -> str:
    return f"""version: 2
agent:
  name: {name}
  model:
    id: test
tools:
  builtins: [read_file]
"""


def test_version_option_prints_version_and_exits() -> None:
    """``--version``/``-V`` print the installed version without loading config."""

    for flag in ("--version", "-V"):
        result = CliRunner().invoke(app, [flag])
        assert result.exit_code == 0
        name, _, version = result.output.strip().partition(" ")
        assert name == "lumen"
        assert version == cli._package_version()


def test_check_config_prints_discovered_tools(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(
        """
version: 2
agent:
  name: cli-test
  model:
    id: test
tools:
  builtins: [read_file]
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config", str(config), "--cwd", str(tmp_path), "--check-config"])

    assert result.exit_code == 0
    assert "Configuration OK" in result.stdout
    assert "read_file" in result.stdout


def test_thinking_option_validates_without_contacting_provider(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text("version: 2\nagent:\n  model: {id: 'openai:gpt-6-astra', api_key: test}\n")
    args = ["--config", str(config), "--cwd", str(tmp_path), "--check-config", "--thinking"]
    result = CliRunner().invoke(app, [*args, "medium"])
    assert result.exit_code == 0, result.output
    assert '"effective": "medium"' in result.output
    assert CliRunner().invoke(app, [*args, "invalid"]).exit_code == 2


def test_dump_effective_config_is_json_and_never_prints_resolved_secrets(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(
        """version: 2
agent:
  model: {id: test, api_key_env: MODEL_TOKEN}
mcp_servers:
  docs:
    transport: stdio
    command: python
    env: {ACCESS_TOKEN: "${MCP_TOKEN}"}
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config", str(config), "--cwd", str(tmp_path), "--dump-effective-config"],
        env={"MODEL_TOKEN": "model-secret-value", "MCP_TOKEN": "mcp-secret-value"},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["effective_config"]["mcp_servers"]["docs"]["env"]["ACCESS_TOKEN"] == (
        "<redacted>"
    )
    assert "model-secret-value" not in result.stdout
    assert "mcp-secret-value" not in result.stdout


def test_capabilities_json_uses_resource_manager_inventory(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(_minimal_config(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["capabilities", "--config", str(config), "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    tools = {item["name"]: item for item in payload["tools"]}
    assert tools["read_file"]["origin"] == "builtin"
    assert tools["read_file"]["schema_digest"].startswith("sha256:")


def test_default_discovery_uses_global_config_from_another_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "another project"
    workspace.mkdir()
    global_config = home / ".lumen" / "agent.yaml"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(_minimal_config(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["--cwd", str(workspace), "--check-config"],
        env={"HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.split("Configuration OK\n", 1)[1])
    assert payload["workspace"] == str(workspace)
    assert payload["session_directory"] == str(workspace / ".lumen" / "sessions")
    assert payload["config_sources"] == [{"scope": "user", "path": str(global_config)}]


def test_noninteractive_project_config_fails_closed_until_trusted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = workspace / ".lumen" / "agent.yaml"
    config.parent.mkdir()
    config.write_text(_minimal_config("project"), encoding="utf-8")
    runner = CliRunner()

    denied = runner.invoke(
        app,
        ["--cwd", str(workspace), "--check-config"],
        env={"HOME": str(home)},
    )
    trusted = runner.invoke(
        app,
        ["trust", "--cwd", str(workspace)],
        env={"HOME": str(home)},
    )
    allowed = runner.invoke(
        app,
        ["--cwd", str(workspace), "--check-config"],
        env={"HOME": str(home)},
    )

    assert denied.exit_code == 1
    assert "project is not trusted" in denied.output
    assert "lumen trust --cwd" in denied.output
    assert trusted.exit_code == 0
    assert allowed.exit_code == 0, allowed.output


def test_init_scopes_refuse_overwrite_and_local_updates_git_exclude(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    (workspace / ".git" / "info").mkdir(parents=True)
    runner = CliRunner()

    project = runner.invoke(
        app,
        ["init", "--cwd", str(workspace)],
        env={"HOME": str(home)},
    )
    repeated = runner.invoke(
        app,
        ["init", "--cwd", str(workspace)],
        env={"HOME": str(home)},
    )
    local = runner.invoke(
        app,
        ["init", "--local", "--cwd", str(workspace)],
        env={"HOME": str(home)},
    )
    global_result = runner.invoke(
        app,
        ["init", "--global", "--cwd", str(workspace)],
        env={"HOME": str(home)},
    )

    assert project.exit_code == 0
    assert repeated.exit_code == 1
    assert "refusing to overwrite" in repeated.output
    assert local.exit_code == 0
    assert global_result.exit_code == 0
    assert (workspace / ".lumen" / "agent.yaml").is_file()
    assert (workspace / ".lumen" / "agent.local.yaml").is_file()
    assert (home / ".lumen" / "agent.yaml").is_file()
    assert "/.lumen/agent.local.yaml" in (workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_mcp_approval_fingerprint_does_not_persist_expanded_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = workspace / ".lumen" / "agent.yaml"
    config.parent.mkdir()
    config.write_text(
        _minimal_config("project")
        + """mcp_servers:
  database:
    transport: stdio
    command: python
    env:
      TOKEN: "${DATABASE_TOKEN}"
""",
        encoding="utf-8",
    )
    environment = {"HOME": str(home), "DATABASE_TOKEN": "actual-database-password"}
    runner = CliRunner()
    assert runner.invoke(app, ["trust", "--cwd", str(workspace)], env=environment).exit_code == 0

    approved = runner.invoke(
        app,
        ["mcp", "approve", "database", "--cwd", str(workspace)],
        env=environment,
    )
    listed = runner.invoke(
        app,
        ["mcp", "list", "--cwd", str(workspace)],
        env=environment,
    )

    assert approved.exit_code == 0, approved.output
    assert listed.exit_code == 0, listed.output
    assert '"approval": "always"' in listed.output
    approval_file = next((home / ".lumen" / "state" / "mcp-approvals").glob("*.json"))
    assert "actual-database-password" not in approval_file.read_text(encoding="utf-8")


def test_disabled_project_mcp_does_not_require_approval(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "version": 2,
            "config_path": tmp_path / "agent.yaml",
            "agent": {"model": {"id": "test"}},
            "mcp_servers": {
                "exa": {
                    "transport": "streamable_http",
                    "url": "https://mcp.exa.ai/mcp",
                    "source_scope": "project",
                    "definition_fingerprint": "sha256:test",
                }
            },
            "mcp": {"enabled": {"exa": False}},
        }
    )

    resolved = cli._apply_mcp_approvals(config, tmp_path, interactive=False)

    assert resolved.mcp_servers["exa"].approval_status == "disabled"
    assert resolved.mcp_diagnostics == (
        {"name": "exa", "scope": "project", "approval": "disabled"},
    )


def test_web_command_refuses_non_loopback_host() -> None:
    result = CliRunner().invoke(app, ["web", "--host", "0.0.0.0", "--api-only"])

    assert result.exit_code == 1
    assert "loopback" in result.output.lower()


def test_web_command_builds_local_asgi_app_without_opening_browser(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(_minimal_config("web-test"), encoding="utf-8")
    captured: dict[str, object] = {}

    def run(application: object, **kwargs: object) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))
    result = CliRunner().invoke(
        app,
        [
            "web",
            "--cwd",
            str(tmp_path),
            "--config",
            str(config),
            "--api-only",
            "--no-open",
            "--port",
            "9876",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:9876/" in result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9876
    assert captured["access_log"] is False


def test_web_command_can_enable_access_log(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(_minimal_config("web-test"), encoding="utf-8")
    captured: dict[str, object] = {}

    def run(_application: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))
    result = CliRunner().invoke(
        app,
        [
            "web",
            "--cwd",
            str(tmp_path),
            "--config",
            str(config),
            "--api-only",
            "--no-open",
            "--access-log",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["access_log"] is True


def test_web_command_starts_background_process(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(_minimal_config("web-test"), encoding="utf-8")
    captured: dict[str, object] = {}

    def run(_application: object, **_kwargs: object) -> None:
        raise AssertionError("the fake detached process must not run in the CLI process")

    def start(_application: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 4321

    def ready(_pid: int, _host: str, _port: int, *, timeout: float = 5.0) -> bool:
        del timeout
        return True

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))
    monkeypatch.setattr(cli, "_background_web_process", start)
    monkeypatch.setattr(cli, "_wait_for_web_start", ready)
    result = CliRunner().invoke(
        app,
        [
            "web",
            "--cwd",
            str(tmp_path),
            "--config",
            str(config),
            "--api-only",
            "--no-open",
            "--background",
            "--port",
            "9877",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "background (PID 4321)" in result.output
    assert str(tmp_path / ".lumen" / "web.log") in result.output
    assert captured["port"] == 9877
    assert captured["access_log"] is False


def test_web_status_reports_workspace_background_process(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state_path = tmp_path / ".lumen" / "web.json"
    log_path = tmp_path / ".lumen" / "web.log"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"pid": 4321, "url": "http://127.0.0.1:8765/", "log": str(log_path)}),
        encoding="utf-8",
    )

    def is_running(pid: int) -> bool:
        return pid == 4321

    monkeypatch.setattr(cli, "_pid_is_running", is_running)

    result = CliRunner().invoke(app, ["web", "--cwd", str(tmp_path), "--status"])

    assert result.exit_code == 0, result.output
    assert "running (PID 4321)" in result.output
    assert "http://127.0.0.1:8765/" in result.output
    assert str(log_path) in result.output


def test_web_stop_terminates_workspace_background_process(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state_path = tmp_path / ".lumen" / "web.json"
    log_path = tmp_path / ".lumen" / "web.log"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"pid": 4321, "url": "http://127.0.0.1:8765/", "log": str(log_path)}),
        encoding="utf-8",
    )
    running = iter((True, False, False))
    sent: list[tuple[int, int | signal.Signals]] = []

    def is_running(_pid: int) -> bool:
        return next(running)

    def kill(pid: int, sig: int | signal.Signals) -> None:
        sent.append((pid, sig))

    monkeypatch.setattr(cli, "_pid_is_running", is_running)
    monkeypatch.setattr(cli.os, "kill", kill)

    result = CliRunner().invoke(app, ["web", "--cwd", str(tmp_path), "--stop"])

    assert result.exit_code == 0, result.output
    assert "Stopped Lumen Web (PID 4321)" in result.output
    assert sent == [(4321, signal.SIGTERM)]
    assert not state_path.exists()


def test_web_process_actions_cannot_be_combined(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["web", "--cwd", str(tmp_path), "--background", "--status", "--api-only"],
    )

    assert result.exit_code == 1
    assert "cannot be combined" in result.output
