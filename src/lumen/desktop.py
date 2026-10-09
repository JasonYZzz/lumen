"""Private desktop bootstrap; runtime commands remain owned by WorkspaceHost."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TextIO
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

from lumen.config import AppConfig, ConfigLoadError, ModelSettingsConfig, resolve_config_environment
from lumen.config_resolver import ConfigResolver, ConfigScope
from lumen.trust import McpApprovalStore, TrustStore

MAX_MESSAGE_BYTES = 65_536
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    type: Literal["reply", "shutdown", "inspect", "end_live"]
    request_id: str = Field(max_length=64)
    value: dict[str, Any] = Field(default_factory=dict, repr=False)


class TrustAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["trust", "cancel"]


class McpAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["once", "always", "deny", "cancel"]


class CredentialAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    values: dict[str, str] = Field(repr=False)


class InitialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_id: str = Field(min_length=1, max_length=200)
    api: Literal["responses", "chat"] = "responses"
    base_url: str | None = Field(default=None, max_length=2048)
    api_key_env: str | None = Field(default=None, max_length=128)


class ControlPipe:
    def __init__(self, reader: asyncio.StreamReader, output: TextIO) -> None:
        self.reader = reader
        self.output = output

    def send(self, kind: str, request_id: str = "", **data: Any) -> None:
        encoded = json.dumps({"version": 1, "type": kind, "request_id": request_id, **data})
        if len(encoded.encode()) > MAX_MESSAGE_BYTES:
            raise ValueError("desktop message exceeds limit")
        self.output.write(encoded + "\n")
        self.output.flush()

    async def read(self) -> Reply:
        line = await self.reader.readline()
        if not line or len(line) > MAX_MESSAGE_BYTES:
            raise EOFError("desktop control channel closed or exceeded limit")
        return Reply.model_validate_json(line)

    async def ask(self, kind: str, **data: Any) -> dict[str, Any]:
        request_id = uuid4().hex
        self.send(kind, request_id, **data)
        reply = await self.read()
        if reply.type == "shutdown":
            raise EOFError("desktop startup cancelled")
        if reply.type != "reply" or reply.request_id != request_id:
            raise ValueError("desktop response does not match current request")
        return reply.value


def configuration_stamp(resolver: ConfigResolver) -> str:
    digest = hashlib.sha256()
    for source in resolver.candidate_sources():
        digest.update(str(source.path).encode())
        try:
            digest.update(source.path.read_bytes())
        except FileNotFoundError:
            digest.update(b"<missing>")
    skills = resolver.workspace / ".lumen" / "skills"
    digest.update(str(skills.is_dir()).encode())
    return digest.hexdigest()


def required_variables(config: AppConfig) -> set[str]:
    names = {model.api_key_env for model in config.agent.model_registry().values() if model.api_key_env}
    for model in config.agent.model_registry().values():
        if model.api_key is None and model.api_key_env is None:
            name = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                    "google": "GOOGLE_API_KEY", "gemini": "GOOGLE_API_KEY"}.get(model.id.split(":")[0])
            if name:
                names.add(name)
    if config.live.enabled:
        routes = list(config.live.routes.values()) or [config.live]
        for route in routes:
            names.add(route.api_key_env)
            workspace_name = getattr(route, "workspace_id_env", None)
            if isinstance(workspace_name, str):
                names.add(workspace_name)
    if config.tools.web.search is not None and config.tools.web.search.api_key_env:
        names.add(config.tools.web.search.api_key_env)
    for name, server in config.mcp_servers.items():
        if not config.mcp.enabled.get(name, True):
            continue
        payload = json.dumps(server.model_dump(mode="json"))
        names.update(match.group(1) for match in _VARIABLE.finditer(payload) if match.group(2) is None)
    names.discard("LUMEN_PROJECT_DIR")
    if any(_NAME.fullmatch(name) is None for name in names):
        raise ConfigLoadError("credential variable name is invalid")
    return names


async def prepare_configuration(
    workspace: Path, pipe: ControlPipe, *, environ: Mapping[str, str], home: Path | None = None,
) -> tuple[AppConfig, dict[str, str]]:
    from lumen.config import create_config_file
    from lumen.trust import apply_mcp_approvals

    variables = dict(environ)
    resolver = ConfigResolver(workspace, environ=variables, home=home)
    store = TrustStore((home or Path.home()) / ".lumen" / "state" / "trust.json")
    stamp = await asyncio.to_thread(configuration_stamp, resolver)
    sources = await asyncio.to_thread(resolver.project_sources)
    skills = await asyncio.to_thread(resolver.has_project_skills)
    if (sources or skills) and not await asyncio.to_thread(store.is_trusted, workspace):
        answer = TrustAnswer.model_validate(await pipe.ask(
            "trust_required", workspace=str(workspace),
            sources=[str(source.path) for source in sources], project_skills=skills,
        ))
        if answer.decision != "trust":
            raise EOFError("project trust declined")
        if await asyncio.to_thread(configuration_stamp, resolver) != stamp:
            raise ConfigLoadError("project configuration changed; reopen the project to review it")
        await asyncio.to_thread(store.trust, workspace)

    if not await asyncio.to_thread(resolver.discovered_sources):
        initial = InitialModel.model_validate(await pipe.ask("configuration_required"))
        model = ModelSettingsConfig(
            id=initial.model_id, api=initial.api, base_url=initial.base_url,
            api_key_env=initial.api_key_env,
        )
        if initial.api_key_env is not None and _NAME.fullmatch(initial.api_key_env) is None:
            raise ConfigLoadError("credential variable name is invalid")
        raw: dict[str, Any] = {
            "version": 2,
            "agent": {"name": "lumen", "model": model.model_dump(mode="json", exclude_none=True)},
            "tools": {"builtins": ["read_file", "list_directory", "search_text"]},
        }
        target = (home or Path.home()) / ".lumen" / "agent.yaml"
        await asyncio.to_thread(resolver.resolve, source_overrides={target: raw}, resolve_environment=False)
        await asyncio.to_thread(create_config_file, target,
                                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))

    stamp = await asyncio.to_thread(configuration_stamp, resolver)
    preview = (await asyncio.to_thread(resolver.resolve, resolve_environment=False)).config
    choices: dict[str, Literal["once", "always", "deny"]] = {}
    approvals = McpApprovalStore(workspace, state_root=(home or Path.home()) / ".lumen" / "state")
    for name, server in preview.mcp_servers.items():
        if (not preview.mcp.enabled.get(name, True)
                or server.source_scope not in {ConfigScope.LEGACY.value, ConfigScope.PROJECT.value}
                or await asyncio.to_thread(approvals.decision, name, server.definition_fingerprint or "")
                is not None):
            continue
        answer = McpAnswer.model_validate(await pipe.ask(
            "mcp_required", name=name, transport=server.transport,
            source=str(server.source_path), fingerprint=server.definition_fingerprint,
        ))
        if answer.decision == "cancel":
            raise EOFError("MCP approval cancelled")
        choices[name] = answer.decision
    if await asyncio.to_thread(configuration_stamp, resolver) != stamp:
        raise ConfigLoadError("configuration changed during approval; reopen the project")

    # Denied servers are excluded before requesting or resolving their credentials.
    preview = await asyncio.to_thread(apply_mcp_approvals, preview, workspace,
                                     interactive=False, choices=choices)
    missing = sorted(required_variables(preview) - variables.keys())
    if missing:
        answer = CredentialAnswer.model_validate(await pipe.ask("credentials_required", names=missing))
        if set(answer.values) != set(missing) or any(not value for value in answer.values.values()):
            raise ConfigLoadError("required credentials were not supplied")
        variables.update(answer.values)
    if await asyncio.to_thread(configuration_stamp, resolver) != stamp:
        raise ConfigLoadError("configuration changed during credential entry; reopen the project")
    config = resolve_config_environment(preview, workspace=workspace, environ=variables)
    return config, variables


async def lifecycle_state(host: Any) -> dict[str, Any]:
    from lumen.application import GetBootstrap, ListSessions

    bootstrap = await host.dispatch(GetBootstrap())
    sessions = await host.dispatch(ListSessions(include_archived=True))
    live_ids: list[str] = []
    blockers: list[str] = []
    for summary in sessions.sessions:
        snapshot = await host.snapshot(summary.session_id)
        if snapshot.pending_effects or snapshot.plan_review_status in {
            "review_pending", "approved_waiting_to_execute",
        }:
            blockers.append(summary.session_id)
        if any(item.get("status") not in {"closed", "not_carried", "imported", "rejected"}
               for item in snapshot.agents):
            blockers.append(summary.session_id)
        for item in snapshot.live_sessions:
            if item.get("connection") not in {"closed", "failed"}:
                identity = item.get("ref", {}).get("id")
                if isinstance(identity, str):
                    live_ids.append(identity)
    return {"active_run_id": bootstrap.active_run_id, "live_ids": live_ids,
            "blockers": sorted(set(blockers))}


def static_directory() -> Path:
    static = Path(__file__).resolve().parent / "api" / "static"
    if not getattr(sys, "frozen", False) and not (static / "index.html").is_file():
        static = Path(__file__).resolve().parents[1] / "web" / "out"
    if not (static / "index.html").is_file():
        raise ConfigLoadError("Web assets missing; build the Web client before starting desktop")
    return static


async def serve(workspace: Path, pipe: ControlPipe) -> None:
    import uvicorn

    from lumen.api import create_web_app
    from lumen.application import CancelRun, EndLiveSession, WorkspaceHost
    from lumen.resources import ResourceManager

    config, variables = await prepare_configuration(workspace, pipe, environ=os.environ)
    static = await asyncio.to_thread(static_directory)
    host = WorkspaceHost(ResourceManager(config, workspace=workspace, environ=variables))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    token = secrets.token_urlsafe(32)
    app = create_web_app(host, launch_token=token, static_dir=static,
                         allowed_hosts={f"127.0.0.1:{port}"})
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", log_config=None))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    incoming = asyncio.create_task(pipe.read())
    try:
        while not server.started:
            if incoming.done():
                incoming.result()
                raise EOFError("desktop startup cancelled")
            if task.done():
                await task
                raise RuntimeError("desktop server failed to start")
            await asyncio.sleep(0.01)
        pipe.send("ready", port=port, token=token)
        while True:
            done, _ = await asyncio.wait({incoming, task}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
                await task
                break
            message = incoming.result()
            if message.type == "inspect":
                pipe.send("state", message.request_id, **await lifecycle_state(host))
            elif message.type == "end_live":
                state = await lifecycle_state(host)
                for identity in state["live_ids"]:
                    await host.dispatch(EndLiveSession(identity))
                pipe.send("state", message.request_id, **await lifecycle_state(host))
            elif message.type == "shutdown":
                state = await lifecycle_state(host)
                if state["active_run_id"]:
                    await host.dispatch(CancelRun(state["active_run_id"]))
                for identity in state["live_ids"]:
                    await host.dispatch(EndLiveSession(identity))
                break
            else:
                raise ValueError("startup reply received after ready")
            incoming = asyncio.create_task(pipe.read())
    finally:
        incoming.cancel()
        await asyncio.gather(incoming, return_exceptions=True)
        server.should_exit = True
        if not server.started:
            task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        finally:
            await host.close()
            listener.close()
            pipe.send("closed")


async def _main(workspace: Path, input_stream: Any, output: TextIO) -> int:
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), input_stream,
    )
    pipe = ControlPipe(reader, output)
    try:
        await serve(workspace, pipe)
        return 0
    except EOFError:
        return 0
    except Exception:
        # Validation errors and third-party exceptions can contain credential input.
        pipe.send("startup_failed", code="startup_failed",
                  message="启动失败。请检查配置和外部工具后重新打开项目。")
        return 1
    finally:
        transport.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumen private desktop backend")
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.cwd.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        parser.error("workspace must be a directory")
    # Keep only dedicated, non-inheritable copies; plugins cannot corrupt the protocol.
    input_stream = os.fdopen(os.dup(0), "rb", buffering=0)
    output = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    with open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
    os.dup2(2, 1)
    raise SystemExit(asyncio.run(_main(workspace, input_stream, output)))


if __name__ == "__main__":
    main()
