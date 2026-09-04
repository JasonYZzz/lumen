from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from lumen.config import PermissionsConfig
from lumen.context.artifacts import ArtifactStore
from lumen.events import ApprovalRequest
from lumen.sessions import SessionRepository
from lumen.skills import SkillLoader
from lumen.tools.gateway import CapabilityApproval, CapabilityGateway, CapabilityInvocation, CapabilityStatus
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.web import build_download_file_spec
from lumen.work_products import EffectStatus, TaskWorkspace


async def test_download_installs_large_skill_without_model_transcription(tmp_path: Path) -> None:
    content = b"---\nname: test-taste\ndescription: Frontend design\n---\n" + b"<Component />\r\n" * 6000
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    workspace = TaskWorkspace(tmp_path, ArtifactStore(tmp_path / "artifacts"), repository)
    workspace.bind_session(session.id)
    spec = build_download_file_spec(
        tmp_path, task_workspace=workspace, host_guard=lambda _: True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)),
    )
    registry = ToolRegistry(tmp_path)
    registry.add(spec, origin="builtin")
    gateway = CapabilityGateway(
        registry, PermissionPolicy(PermissionsConfig()), default_timeout=2,
        effect_recorder=workspace.record_tool_effect,
    )
    path = ".lumen/skills/test-taste/SKILL.md"
    invocation = CapabilityInvocation(
        execution_id="run", provider_call_id="download", name="download_file",
        arguments={"url": "https://example.com/SKILL.md", "path": path},
    )
    async def approve(_: ApprovalRequest) -> CapabilityApproval:
        return CapabilityApproval(approved=True)

    result = await gateway.invoke(invocation, approve=approve)
    assert result.status is CapabilityStatus.SUCCEEDED
    assert (tmp_path / path).read_bytes() == content
    assert len(result.model_output or "") < 500
    assert hashlib.sha256(content).hexdigest() in (result.model_output or "")
    project_skills = [skill.name for skill in SkillLoader(tmp_path).discover() if skill.source == "project"]
    assert project_skills == ["test-taste"]
    effects = repository.load(session.id).work_state.effects
    assert len(effects) == 1
    assert effects[0].status is EffectStatus.VERIFIED
    assert not workspace.completion_blockers(session.id)


@pytest.mark.parametrize(
    "failure", ["size", "hash", "binary", "html", "http", "redirect", "escape", "exists"],
)
async def test_download_failures_do_not_publish(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "source.md"
    if failure == "exists":
        path.write_text("user content")
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "http":
            return httpx.Response(404)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        return httpx.Response(
            200, content=b"\xff\x00" if failure == "binary" else b"source" * 100,
            headers={"content-type": "text/html" if failure == "html" else "text/plain"},
        )

    spec = build_download_file_spec(
        tmp_path, max_bytes=10 if failure == "size" else 1024,
        host_guard=lambda host: host == "example.com", transport=httpx.MockTransport(respond),
    )
    with pytest.raises((ValueError, httpx.HTTPStatusError, FileExistsError)):
        await spec.function(
            "https://example.com/source", "../escape.md" if failure == "escape" else "source.md",
            sha256="0" * 64 if failure == "hash" else None,
        )
    assert path.read_text() == "user content" if failure == "exists" else not path.exists()
    if failure in {"escape", "exists"}:
        assert calls == 0


async def test_download_cancellation_during_network_does_not_write(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def respond(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, content=b"never")

    spec = build_download_file_spec(
        tmp_path, host_guard=lambda _: True, transport=httpx.MockTransport(respond),
    )
    task = asyncio.create_task(spec.function("https://example.com/source", "source.md"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "source.md").exists()


async def test_download_requires_approval_before_network(tmp_path: Path) -> None:
    calls: list[Any] = []
    spec = build_download_file_spec(
        tmp_path, host_guard=lambda _: True,
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    registry = ToolRegistry(tmp_path)
    registry.add(spec, origin="builtin")
    gateway = CapabilityGateway(registry, PermissionPolicy(PermissionsConfig()), default_timeout=2)
    result = await gateway.invoke(CapabilityInvocation(
        execution_id="run", provider_call_id="download", name="download_file",
        arguments={"url": "https://example.com/source", "path": "source.md"},
    ))
    assert not result.succeeded
    assert not calls
    assert not (tmp_path / "source.md").exists()
    assert len(json.dumps(result.output)) < 1000
