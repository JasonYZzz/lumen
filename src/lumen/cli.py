from __future__ import annotations

import asyncio
import importlib.metadata
import ipaddress
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlencode

import typer

from lumen.api import create_web_app
from lumen.application import WorkspaceHost
from lumen.approval import ApprovalMode
from lumen.branding import FRAMEWORK_NAME, FRAMEWORK_SLUG
from lumen.config import AppConfig, ConfigLoadError
from lumen.config_resolver import ConfigResolution, ConfigResolver, ConfigScope
from lumen.headless import run_headless
from lumen.reasoning import ReasoningLevel
from lumen.resources import ResourceManager
from lumen.trust import McpApprovalStore, TrustStore, git_common_directory
from lumen.ui.app import LumenApp

app = typer.Typer(
    name=FRAMEWORK_SLUG,
    help=f"Run {FRAMEWORK_NAME}, a configurable tool-using agent framework, in a terminal UI.",
    invoke_without_command=True,
)
mcp_app = typer.Typer(help="Inspect and manage project MCP server approvals.")
app.add_typer(mcp_app, name="mcp")


def _package_version() -> str:
    """Resolve the installed ``lumen-agent`` version, falling back to the
    pyproject version when the distribution metadata is unavailable (for
    example when running from a bare source checkout)."""

    try:
        return importlib.metadata.version("lumen-agent")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _version_callback(value: bool) -> None:
    """Eager ``--version`` callback: print the version and exit before any
    configuration loading runs."""

    if value:
        typer.echo(f"{FRAMEWORK_SLUG} {_package_version()}")
        raise typer.Exit


_FULL_TEMPLATE = """version: 2
agent:
  name: lumen
  model:
    id: openai:gpt-5
    api_key_env: OPENAI_API_KEY
    reasoning_effort: medium
tools:
  builtins:
    - read_file
    - list_directory
    - search_text
permissions:
  default_mode: manual
"""

_LOCAL_TEMPLATE = """version: 2
# Local-only overrides. This file should not be committed.
# agent:
#   limits:
#     request_count: 75
"""


async def _check_resources(manager: ResourceManager) -> dict[str, object]:
    async with manager:
        return manager.summary()


def _workspace(path: Path) -> Path:
    workspace = path.expanduser().resolve()
    if not workspace.is_dir():
        raise ConfigLoadError(f"workspace is not a directory: {workspace}")
    return workspace


def _web_runtime_paths(workspace: Path) -> tuple[Path, Path]:
    runtime_dir = workspace / ".lumen"
    return runtime_dir / "web.json", runtime_dir / "web.log"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Unlike POSIX, Windows os.kill(pid, 0) terminates the target process.
        raise ConfigLoadError("background Web process management is supported on macOS and Linux")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_web_state(state_path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _write_web_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(state_path)


def _remove_web_state(state_path: Path, *, pid: int | None = None) -> None:
    state = _read_web_state(state_path)
    if pid is not None and state is not None and state.get("pid") != pid:
        return
    state_path.unlink(missing_ok=True)


def _background_web_process(
    application: object,
    *,
    host: str,
    port: int,
    access_log: bool,
    state_path: Path,
    log_path: Path,
    base_url: str,
    serve: Callable[..., Any],
) -> int:
    if sys.platform == "win32" or not hasattr(os, "fork"):
        raise ConfigLoadError("lumen web --background is supported on macOS and Linux")
    existing = _read_web_state(state_path)
    existing_pid = int(existing.get("pid", 0)) if existing is not None else 0
    if _pid_is_running(existing_pid):
        raise ConfigLoadError(
            f"Lumen Web is already running for this workspace (PID {existing_pid})"
        )
    try:
        with socket.create_connection((host, port), timeout=0.2):
            pass
    except OSError:
        pass
    else:
        raise ConfigLoadError(f"cannot start Lumen Web: {host}:{port} is already in use")
    _remove_web_state(state_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    log_path.chmod(0o600)

    launch_read, launch_write = os.pipe()
    pid = os.fork()
    if pid:
        os.close(launch_read)
        try:
            _write_web_state(
                state_path,
                {
                    "pid": pid,
                    "url": base_url,
                    "log": str(log_path),
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )
            os.write(launch_write, b"1")
        finally:
            os.close(launch_write)
        return pid

    os.close(launch_write)
    try:
        may_start = os.read(launch_read, 1) == b"1"
    finally:
        os.close(launch_read)
    if not may_start:
        os._exit(1)

    exit_code = 0
    try:
        os.setsid()
        with Path(os.devnull).open("rb") as null_input, log_path.open("ab", buffering=0) as log:
            os.dup2(null_input.fileno(), 0)
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            serve(
                application,
                host=host,
                port=port,
                log_level="info",
                access_log=access_log,
            )
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    finally:
        _remove_web_state(state_path, pid=os.getpid())
    os._exit(exit_code)


def _wait_for_web_start(pid: int, host: str, port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return False
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _web_status(workspace: Path) -> bool:
    state_path, _ = _web_runtime_paths(workspace)
    state = _read_web_state(state_path)
    pid = int(state.get("pid", 0)) if state is not None else 0
    if state is None or not _pid_is_running(pid):
        _remove_web_state(state_path)
        typer.echo("Lumen Web is not running for this workspace")
        return False
    typer.echo(f"Lumen Web is running (PID {pid})")
    typer.echo(f"URL: {state.get('url', '-')}")
    typer.echo(f"Log: {state.get('log', '-')}")
    return True


def _stop_web(workspace: Path, *, timeout: float = 5.0) -> bool:
    state_path, _ = _web_runtime_paths(workspace)
    state = _read_web_state(state_path)
    pid = int(state.get("pid", 0)) if state is not None else 0
    if state is None or not _pid_is_running(pid):
        _remove_web_state(state_path)
        typer.echo("Lumen Web is not running for this workspace")
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_is_running(pid):
        time.sleep(0.05)
    if _pid_is_running(pid):
        typer.echo(f"Stop requested for Lumen Web (PID {pid}); it is still shutting down")
        return True
    _remove_web_state(state_path, pid=pid)
    typer.echo(f"Stopped Lumen Web (PID {pid})")
    return True


def _ensure_project_trust(resolver: ConfigResolver, workspace: Path) -> bool:
    project_sources = resolver.project_sources()
    has_skills = resolver.has_project_skills()
    if resolver.exclusive or (not project_sources and not has_skills):
        return True

    store = TrustStore()
    if store.is_trusted(workspace):
        return True

    material = [str(source.path) for source in project_sources]
    if has_skills:
        material.append(str(workspace / ".lumen" / "skills"))
    listed = "\n".join(f"  - {item}" for item in material)
    if not sys.stdin.isatty():
        raise ConfigLoadError(
            f"project is not trusted; refusing to load:\n{listed}\nRun 'lumen trust --cwd {workspace}' first."
        )
    typer.echo(f"This project contains Lumen-controlled files:\n{listed}")
    if not typer.confirm(f"Trust project {workspace}?"):
        raise ConfigLoadError("project was not trusted")
    store.trust(workspace)
    return True


def _apply_mcp_approvals(
    config: AppConfig,
    workspace: Path,
    *,
    interactive: bool,
    report_only: bool = False,
) -> AppConfig:
    store = McpApprovalStore(workspace)
    accepted = {}
    warnings = list(config.config_warnings)
    diagnostics: list[dict[str, str]] = []
    for name, server in config.mcp_servers.items():
        if not config.mcp.enabled.get(name, True):
            accepted[name] = server.model_copy(update={"approval_status": "disabled"})
            diagnostics.append(
                {"name": name, "scope": server.source_scope, "approval": "disabled"}
            )
            continue
        if server.source_scope not in {ConfigScope.LEGACY.value, ConfigScope.PROJECT.value}:
            accepted[name] = server.model_copy(update={"approval_status": "automatic"})
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "automatic"})
            continue
        fingerprint = server.definition_fingerprint or ""
        decision = store.decision(name, fingerprint)
        if decision == "always":
            accepted[name] = server.model_copy(update={"approval_status": "always"})
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "always"})
            continue
        if decision == "deny":
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "deny"})
            warnings.append(
                f"MCP server {name!r} from {server.source_scope} scope is denied and was not started."
            )
            continue
        if report_only:
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "pending"})
            warnings.append(
                f"MCP server {name!r} from {server.source_scope} scope is pending approval; "
                f"run 'lumen mcp approve {name}'."
            )
            continue
        if not interactive:
            raise ConfigLoadError(
                f"MCP server {name!r} from {server.source_scope} scope requires approval. "
                f"Run 'lumen mcp approve {name}' or 'lumen mcp deny {name}'."
            )
        choice = (
            typer.prompt(
                f"Allow project MCP server {name!r} ({server.transport})? [once/always/deny]",
                default="once",
            )
            .strip()
            .lower()
        )
        if choice not in {"once", "always", "deny"}:
            raise ConfigLoadError("MCP approval must be one of: once, always, deny")
        if choice == "deny":
            store.set(name, fingerprint, "deny")
            diagnostics.append({"name": name, "scope": server.source_scope, "approval": "deny"})
            warnings.append(f"MCP server {name!r} was denied and was not started.")
            continue
        if choice == "always":
            store.set(name, fingerprint, "always")
        accepted[name] = server.model_copy(update={"approval_status": choice})
        diagnostics.append({"name": name, "scope": server.source_scope, "approval": choice})
    return config.model_copy(
        update={
            "mcp_servers": accepted,
            "config_warnings": tuple(warnings),
            "mcp_diagnostics": tuple(diagnostics),
        }
    )


def _resolve_for_workspace(
    workspace: Path,
    config_path: Path | None,
    *,
    approve_mcp: bool,
    report_only: bool = False,
) -> AppConfig:
    config, _resolution = _resolve_with_report(
        workspace,
        config_path,
        approve_mcp=approve_mcp,
        report_only=report_only,
    )
    return config


def _resolve_with_report(
    workspace: Path,
    config_path: Path | None,
    *,
    approve_mcp: bool,
    report_only: bool = False,
) -> tuple[AppConfig, ConfigResolution]:
    resolver = ConfigResolver(workspace, explicit_path=config_path)
    trusted = _ensure_project_trust(resolver, workspace)
    resolution = resolver.resolve(project_trusted=trusted)
    config = _apply_mcp_approvals(
        resolution.config,
        workspace,
        interactive=approve_mcp and sys.stdin.isatty(),
        report_only=report_only,
    )
    return config, resolution


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Use one explicit agent YAML file (overrides LUMEN_CONFIG and layered discovery).",
        ),
    ] = None,
    cwd: Annotated[
        Path,
        typer.Option("--cwd", help="Workspace root exposed to built-in file tools."),
    ] = Path("."),
    resume: Annotated[
        str | None,
        typer.Option("--resume", help="Session UUID to resume at startup."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help="Logical model name to select at startup (must be a key under agent.models).",
        ),
    ] = None,
    thinking: Annotated[
        ReasoningLevel | None, typer.Option("--thinking", help="Reasoning effort for this Session."),
    ] = None,
    check_config: Annotated[
        bool,
        typer.Option("--check-config", help="Validate configuration and discover approved tools, then exit."),
    ] = False,
    dump_effective_config: Annotated[
        bool,
        typer.Option(
            "--dump-effective-config",
            help="Print the merged, secret-redacted configuration with field provenance, then exit.",
        ),
    ] = False,
    print_prompt: Annotated[
        str | None,
        typer.Option(
            "--print",
            "-p",
            help="Run one non-interactive agent turn and print the final answer, then exit.",
        ),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--output-format", help="Output format for --print: text or json."),
    ] = "text",
    permission_mode: Annotated[
        str | None,
        typer.Option(
            "--permission-mode",
            help="Approval mode for --print: manual (deny all), accept_edits, or auto.",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Lumen version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Launch Lumen or validate its complete runtime configuration."""

    if ctx.invoked_subcommand is not None:
        return
    try:
        if sum((print_prompt is not None, check_config, dump_effective_config)) > 1:
            raise ConfigLoadError(
                "--print, --check-config, and --dump-effective-config cannot be combined"
            )
        if print_prompt is None and (permission_mode is not None or output_format != "text"):
            raise ConfigLoadError("--output-format and --permission-mode require --print")
        if output_format not in {"text", "json"}:
            raise ConfigLoadError("--output-format must be one of: text, json")
        headless_mode: ApprovalMode | None = None
        if permission_mode is not None:
            if permission_mode not in {mode.value for mode in ApprovalMode}:
                raise ConfigLoadError(
                    "--permission-mode must be one of: manual, accept_edits, auto"
                )
            headless_mode = ApprovalMode(permission_mode)
        workspace = _workspace(cwd)
        config, resolution = _resolve_with_report(
            workspace,
            config_path,
            approve_mcp=not check_config and not dump_effective_config,
            report_only=check_config or dump_effective_config,
        )
        if dump_effective_config:
            typer.echo(
                json.dumps(
                    resolution.report(config=config).as_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        manager = ResourceManager(config, workspace=workspace)
        if model is not None:
            try:
                manager.set_startup_model(model)
            except KeyError as error:
                raise ConfigLoadError(str(error)) from error
        if thinking is not None:
            manager.set_startup_reasoning(thinking)
        if check_config:
            summary = asyncio.run(_check_resources(manager))
            typer.echo("Configuration OK")
            typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        if print_prompt is not None:
            result = asyncio.run(
                run_headless(
                    manager,
                    print_prompt,
                    resume_id=resume,
                    permission_mode=headless_mode,
                    output_format=output_format,
                )
            )
            if result.error is not None:
                typer.echo(f"Error: {result.error}", err=True)
            raise typer.Exit(result.exit_code())
        LumenApp(config, manager, resume_id=resume).run()
    except typer.Exit:
        raise
    except (ConfigLoadError, OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


@app.command("web")
def web(
    cwd: Annotated[Path, typer.Option("--cwd", help="Workspace exposed to Lumen tools.")] = Path("."),
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    thinking: Annotated[ReasoningLevel | None, typer.Option("--thinking")] = None,
    resume: Annotated[str | None, typer.Option("--resume")] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open")] = False,
    api_only: Annotated[bool, typer.Option("--api-only")] = False,
    background: Annotated[
        bool,
        typer.Option("--background", "-d", help="Run detached and write logs under .lumen/."),
    ] = False,
    status: Annotated[bool, typer.Option("--status", help="Show the background Web process.")] = False,
    stop: Annotated[bool, typer.Option("--stop", help="Stop the background Web process.")] = False,
    access_log: Annotated[
        bool,
        typer.Option("--access-log", help="Log every HTTP request, including cache hits."),
    ] = False,
) -> None:
    """Launch the local, single-workspace Lumen Web client."""

    try:
        if sum((background, status, stop)) > 1:
            raise ConfigLoadError("--background, --status, and --stop cannot be combined")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ConfigLoadError("lumen web --host must be a loopback IP address") from error
        if not address.is_loopback:
            raise ConfigLoadError("lumen web only accepts a loopback host in this release")

        workspace = _workspace(cwd)
        if status:
            _web_status(workspace)
            return
        if stop:
            _stop_web(workspace)
            return
        config = _resolve_for_workspace(workspace, config_path, approve_mcp=True)
        manager = ResourceManager(config, workspace=workspace)
        if model is not None:
            manager.set_startup_model(model)

        if thinking is not None:
            manager.set_startup_reasoning(thinking)
        static_dir = Path(__file__).resolve().parent / "api" / "static"
        source_export = Path(__file__).resolve().parents[1] / "web" / "out"
        if not static_dir.joinpath("index.html").is_file() and source_export.joinpath("index.html").is_file():
            static_dir = source_export
        if not api_only and not static_dir.joinpath("index.html").is_file():
            raise ConfigLoadError(
                "Lumen Web assets are missing; run `pnpm --dir src/web build` or use --api-only"
            )

        launch_token = secrets.token_urlsafe(32)
        url_host = f"[{host}]" if address.version == 6 else host
        application = create_web_app(
            WorkspaceHost(manager),
            launch_token=launch_token,
            static_dir=static_dir,
            api_only=api_only,
            allowed_hosts={f"{url_host}:{port}", url_host},
        )
        query = {"token": launch_token}
        if resume is not None:
            query["session"] = resume
        url = f"http://{url_host}:{port}/?{urlencode(query)}"
        import uvicorn

        if background:
            state_path, log_path = _web_runtime_paths(workspace)
            pid = _background_web_process(
                application,
                host=host,
                port=port,
                access_log=access_log,
                state_path=state_path,
                log_path=log_path,
                base_url=f"http://{url_host}:{port}/",
                serve=uvicorn.run,
            )
            if not _wait_for_web_start(pid, host, port):
                _remove_web_state(state_path, pid=pid)
                raise ConfigLoadError(f"background Web process exited; inspect {log_path}")
            typer.echo(f"Lumen Web started in background (PID {pid})")
            typer.echo(f"Lumen Web: {url}")
            typer.echo(f"Log: {log_path}")
            typer.echo(f"Stop: lumen web --cwd {workspace} --stop")
            if not no_open and not api_only:
                webbrowser.open(url)
            return

        typer.echo(f"Lumen Web: {url}")
        if not no_open and not api_only:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        uvicorn.run(
            application,
            host=host,
            port=port,
            log_level="info",
            access_log=access_log,
        )
    except (ConfigLoadError, OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


@app.command("capabilities")
def capabilities(
    cwd: Annotated[Path, typer.Option("--cwd", help="Workspace used for capability discovery.")] = Path("."),
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Inspect effective tools, Skills, MCP servers, and Agent profiles."""

    try:
        workspace = _workspace(cwd)
        config = _resolve_for_workspace(
            workspace,
            config_path,
            approve_mcp=False,
            report_only=True,
        )
        manager = ResourceManager(config, workspace=workspace)
        if model is not None:
            manager.set_startup_model(model)

        async def collect() -> dict[str, Any]:
            async with manager:
                return manager.capabilities_report()

        report = asyncio.run(collect())
        if json_output:
            typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
            return
        for item in report["tools"]:
            typer.echo(
                f"{item['name']}\t{item['status']}\t{item['approval']}\t{item['origin']}"
            )
    except (ConfigLoadError, OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


def _write_new_file(path: Path, content: str) -> None:
    if path.exists():
        raise ConfigLoadError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _exclude_local_config(workspace: Path) -> Path | None:
    common = git_common_directory(workspace)
    if common is None:
        return None
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    rule = "/.lumen/agent.local.yaml"
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if rule not in existing:
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and existing[-1] != "":
                handle.write("\n")
            handle.write(f"{rule}\n")
    return exclude


@app.command("init")
def init_config(
    cwd: Annotated[Path, typer.Option("--cwd", help="Project directory.")] = Path("."),
    global_: Annotated[
        bool,
        typer.Option("--global", help="Create the user configuration under ~/.lumen."),
    ] = False,
    local: Annotated[
        bool,
        typer.Option("--local", help="Create a local-only project override."),
    ] = False,
) -> None:
    """Create a configuration template without overwriting existing files."""

    try:
        workspace: Path | None = None
        if global_ and local:
            raise ConfigLoadError("--global and --local cannot be combined")
        if global_:
            target = Path.home() / ".lumen" / "agent.yaml"
            content = _FULL_TEMPLATE
        else:
            workspace = _workspace(cwd)
            if local:
                target = workspace / ".lumen" / "agent.local.yaml"
                content = _LOCAL_TEMPLATE
            else:
                target = workspace / ".lumen" / "agent.yaml"
                content = _FULL_TEMPLATE
        _write_new_file(target, content)
        typer.echo(f"Created {target}")
        if local:
            assert workspace is not None
            exclude = _exclude_local_config(workspace)
            if exclude is not None:
                typer.echo(f"Added /.lumen/agent.local.yaml to {exclude}")
    except (ConfigLoadError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


@app.command("trust")
def trust_project(
    cwd: Annotated[Path, typer.Option("--cwd", help="Project directory.")] = Path("."),
    revoke: Annotated[bool, typer.Option("--revoke", help="Revoke trust for this project.")] = False,
) -> None:
    """Trust or revoke trust for a project directory."""

    try:
        workspace = _workspace(cwd)
        store = TrustStore()
        if revoke:
            changed = store.revoke(workspace)
            typer.echo(f"Trust {'revoked' if changed else 'was not recorded'}: {workspace}")
        else:
            project_id = store.trust(workspace)
            typer.echo(f"Trusted {workspace} ({project_id})")
    except OSError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


def _mcp_config(cwd: Path, config_path: Path | None) -> tuple[Path, AppConfig]:
    workspace = _workspace(cwd)
    resolver = ConfigResolver(workspace, explicit_path=config_path)
    if not resolver.exclusive and (resolver.project_sources() or resolver.has_project_skills()):
        if not TrustStore().is_trusted(workspace):
            raise ConfigLoadError(f"project is not trusted; run 'lumen trust --cwd {workspace}' first")
    return workspace, resolver.resolve(project_trusted=True).config


@mcp_app.command("list")
def mcp_list(
    cwd: Annotated[Path, typer.Option("--cwd", help="Project directory.")] = Path("."),
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """List effective MCP definitions, scopes, and approval status."""

    try:
        workspace, config = _mcp_config(cwd, config_path)
        store = McpApprovalStore(workspace)
        rows: list[dict[str, str | None]] = []
        for name, server in config.mcp_servers.items():
            if server.source_scope in {ConfigScope.LEGACY.value, ConfigScope.PROJECT.value}:
                status = store.decision(name, server.definition_fingerprint or "") or "pending"
            else:
                status = "automatic"
            rows.append(
                {
                    "name": name,
                    "scope": server.source_scope,
                    "transport": server.transport,
                    "approval": status,
                    "source": str(server.source_path) if server.source_path else None,
                }
            )
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
    except (ConfigLoadError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


def _set_mcp_decision(
    name: str,
    decision: str,
    cwd: Path,
    config_path: Path | None,
) -> None:
    workspace, config = _mcp_config(cwd, config_path)
    try:
        server = config.mcp_servers[name]
    except KeyError as error:
        raise ConfigLoadError(
            f"unknown MCP server {name!r}; configured: {sorted(config.mcp_servers)}"
        ) from error
    if server.source_scope not in {ConfigScope.LEGACY.value, ConfigScope.PROJECT.value}:
        raise ConfigLoadError(
            f"MCP server {name!r} is {server.source_scope}-scoped and does not require approval"
        )
    McpApprovalStore(workspace).set(
        name,
        server.definition_fingerprint or "",
        "always" if decision == "always" else "deny",
    )
    typer.echo(f"MCP server {name!r}: {decision}")


@mcp_app.command("approve")
def mcp_approve(
    name: str,
    cwd: Annotated[Path, typer.Option("--cwd")] = Path("."),
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Always approve the current fingerprint of a project MCP server."""

    try:
        _set_mcp_decision(name, "always", cwd, config_path)
    except (ConfigLoadError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


@mcp_app.command("deny")
def mcp_deny(
    name: str,
    cwd: Annotated[Path, typer.Option("--cwd")] = Path("."),
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Deny the current fingerprint of a project MCP server."""

    try:
        _set_mcp_decision(name, "deny", cwd, config_path)
    except (ConfigLoadError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


@mcp_app.command("reset")
def mcp_reset(
    name: str | None = None,
    cwd: Annotated[Path, typer.Option("--cwd")] = Path("."),
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Clear one or all persistent MCP decisions for this project."""

    try:
        # Resolve through the same workspace/trust path as the other mcp
        # subcommands so --config selects the same project context.
        workspace, _ = _mcp_config(cwd, config_path)
        changed = McpApprovalStore(workspace).reset(name)
        target = repr(name) if name else "all servers"
        typer.echo(f"Reset {target}" if changed else f"No approval stored for {target}")
    except (ConfigLoadError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


if __name__ == "__main__":
    app()
