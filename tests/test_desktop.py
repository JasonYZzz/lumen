from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from lumen.config import ConfigLoadError, create_config_file
from lumen.config_resolver import ConfigResolver
from lumen.desktop import ControlPipe, Reply, prepare_configuration, required_variables
from lumen.trust import TrustStore


class Answers(ControlPipe):
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def ask(self, kind: str, **data: Any) -> dict[str, Any]:
        self.requests.append((kind, data))
        return self.answers.pop(0)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LUMEN_CONFIG", raising=False)
    return workspace, home


async def test_credentials_are_explicit_and_inline_values_survive(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, home = project
    create_config_file(home / ".lumen/agent.yaml", "version: 2\nagent:\n  models:\n"
                       "    a: {id: 'openai:example', api_key_env: CUSTOM_KEY}\n"
                       "    b: {id: 'anthropic:example', api_key: inline-value}\n  default_model: a\n")
    monkeypatch.setenv("CUSTOM_KEY", "unrelated-process-value")
    answers = Answers([{"values": {"CUSTOM_KEY": "private-value"}}])
    config, variables = await prepare_configuration(workspace, answers, environ={}, home=home)  # type: ignore[arg-type]
    assert config.agent.models["a"].api_key == "private-value"
    assert config.agent.models["b"].api_key == "inline-value"
    assert variables == {"CUSTOM_KEY": "private-value"}
    assert os.environ["CUSTOM_KEY"] == "unrelated-process-value"
    assert "private-value" not in config.model_dump_json()


async def test_denied_and_disabled_mcp_do_not_request_or_expand_credentials(
    project: tuple[Path, Path],
) -> None:
    workspace, home = project
    create_config_file(workspace / "agent.yaml", "version: 2\nagent: {model: {id: test}}\n"
                       "mcp_servers:\n  denied: {transport: stdio, command: example, "
                       "env: {TOKEN: '${NEVER_REQUEST}'}}\n"
                       "  disabled: {transport: stdio, command: example, args: ['${ALSO_NOT_REQUESTED}']}\n"
                       "mcp: {enabled: {disabled: false}}\n")
    answers = Answers([{"decision": "trust"}, {"decision": "deny"}])
    config, _ = await prepare_configuration(workspace, answers, environ={}, home=home)  # type: ignore[arg-type]
    assert [kind for kind, _ in answers.requests] == ["trust_required", "mcp_required"]
    assert set(config.mcp_servers) == {"disabled"}
    assert required_variables(config) == set()
    assert TrustStore(home / ".lumen/state/trust.json").is_trusted(workspace)


async def test_no_config_creates_private_file_without_overwriting(project: tuple[Path, Path]) -> None:
    workspace, home = project
    answers = Answers([{"model_id": "test"}])
    config, _ = await prepare_configuration(workspace, answers, environ={}, home=home)  # type: ignore[arg-type]
    assert config.agent.model is not None and config.agent.model.id == "test"
    target = home / ".lumen/agent.yaml"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    content = target.read_bytes()
    with pytest.raises(ConfigLoadError, match="overwrite"):
        create_config_file(target, "replacement")
    assert target.read_bytes() == content


async def test_trust_cancellation_never_loads_invalid_project(project: tuple[Path, Path]) -> None:
    workspace, home = project
    create_config_file(workspace / "agent.yaml", "not valid: [")
    with pytest.raises(EOFError):
        await prepare_configuration(workspace, Answers([{"decision": "cancel"}]),  # type: ignore[arg-type]
                                    environ={}, home=home)
    assert not TrustStore(home / ".lumen/state/trust.json").is_trusted(workspace)


async def test_configuration_change_during_trust_is_rejected(project: tuple[Path, Path]) -> None:
    workspace, home = project
    target = workspace / "agent.yaml"
    create_config_file(target, "version: 2\nagent: {model: {id: test}}\n")

    class Changed(Answers):
        async def ask(self, kind: str, **data: Any) -> dict[str, Any]:
            await asyncio.to_thread(target.write_text, "version: 2\nagent: {model: {id: 'openai:changed'}}\n")
            return {"decision": "trust"}

    with pytest.raises(ConfigLoadError, match="changed"):
        await prepare_configuration(workspace, Changed([]), environ={}, home=home)
    assert not TrustStore(home / ".lumen/state/trust.json").is_trusted(workspace)


def test_injected_mcp_credentials_are_redacted_from_urls_and_arguments(project: tuple[Path, Path]) -> None:
    workspace, home = project
    create_config_file(home / ".lumen/agent.yaml", "version: 2\nagent: {model: {id: test}}\n"
                       "mcp_servers:\n  service:\n    transport: stdio\n    command: example\n"
                       "    args: ['--credential=${CUSTOM_VALUE}']\n")
    resolution = ConfigResolver(workspace, home=home, environ={"CUSTOM_VALUE": "private-example"}).resolve()
    assert resolution.config.mcp_servers["service"].args == ["--credential=private-example"]
    assert "private-example" not in json.dumps(resolution.report().as_dict())


async def test_pipe_rejects_mismatched_requests_and_extra_fields() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"version":1,"type":"reply","request_id":"stale","value":{}}\n')
    with pytest.raises(ValueError, match="match"):
        await ControlPipe(reader, io.StringIO()).ask("trust_required")
    with pytest.raises(ValueError):
        Reply.model_validate({"version": 1, "type": "shutdown", "request_id": "", "command": "exec"})


def test_resolver_map_has_no_ambient_fallback(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, home = project
    create_config_file(home / ".lumen/agent.yaml", "version: 2\nagent: {model: {id: test}}\n"
                       "mcp_servers: {remote: {transport: stdio, command: example, "
                       "env: {TOKEN: '${HIDDEN_KEY}'}}}\n")
    monkeypatch.setenv("HIDDEN_KEY", "ambient-secret")
    resolver = ConfigResolver(workspace, home=home, environ={})
    preview = resolver.resolve(resolve_environment=False).config
    assert preview.mcp_servers["remote"].env["TOKEN"] == "${HIDDEN_KEY}"
    with pytest.raises(ConfigLoadError, match="HIDDEN_KEY"):
        resolver.resolve()


@pytest.mark.skipif(sys.platform != "darwin", reason="Desktop pipe transport is macOS-only")
async def test_real_backend_cookie_origin_shutdown_and_parent_eof(project: tuple[Path, Path]) -> None:
    from lumen.desktop import static_directory

    try:
        static_directory()
    except ConfigLoadError:
        pytest.skip("Web production assets must be built for desktop integration")
    workspace, home = project
    create_config_file(home / ".lumen/agent.yaml", "version: 2\nagent: {model: {id: test}}\n")
    environment = {name: value for name, value in os.environ.items() if name in {"PATH", "HOME", "LANG"}}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "lumen.desktop", "--cwd", str(workspace), env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None and process.stdin is not None
    try:
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
        assert ready["type"] == "ready"
        url = f'http://127.0.0.1:{ready["port"]}'
        async with httpx.AsyncClient(base_url=url, follow_redirects=True) as client:
            assert (await client.get("/api/v1/bootstrap")).status_code == 401
            response = await client.get("/", params={"token": ready["token"]})
            assert response.status_code == 200 and "token=" not in str(response.url)
            assert (await client.get("/api/v1/bootstrap")).status_code == 200
            response = await client.post("/api/v1/sessions", json={}, headers={"Origin": "https://evil.test"})
            assert response.status_code == 403
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 20)
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
