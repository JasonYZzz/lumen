from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_reasoning import manager
from test_web_api import ApiResources
from test_workspace_host import LocalResources, _collect_run, _runtime  # pyright: ignore[reportPrivateUsage]

from lumen.agents.types import AgentExecutionResult, AgentThreadRef, AgentThreadState
from lumen.api import create_web_app
from lumen.application import (
    CreateSession,
    InvalidStateError,
    SetApprovalMode,
    SetPermissionPreset,
    StartRun,
    WorkspaceBusyError,
    WorkspaceHost,
)
from lumen.collaboration import SessionSettingsState
from lumen.config import LimitsConfig, PermissionsConfig, SandboxConfig
from lumen.runtime import AgentRuntime
from lumen.sandbox import SandboxRunner, SandboxUnavailableError, sandbox_scope, session_sandbox_mode
from lumen.tools.builtin import build_builtin_specs
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.workspace import Workspace, WorkspaceViolation
from lumen.work_products import TaskWorkspace


async def test_permission_presets_apply_to_real_file_tools_without_changing_other_sessions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = tmp_path / "external.txt"
    resources = LocalResources(root, _runtime())
    work = TaskWorkspace(root, resources.artifact_store, resources.session_repository)
    resources.task_workspace = work
    registry = ToolRegistry(root)
    registry.add_many(build_builtin_specs(root, sandbox_config=SandboxConfig(), task_workspace=work),
                      origin="builtin")
    policy = PermissionPolicy(PermissionsConfig())
    gateway = CapabilityGateway(registry, policy, default_timeout=5)
    resources.capability_gateway = gateway

    phase = 0

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        from pydantic_ai.models.function import DeltaToolCall

        nonlocal phase
        phase += 1
        if phase > 1:
            yield "done"
        else:
            yield {0: DeltaToolCall(name="write_file", json_args=json.dumps({
                "path": str(target), "content": "verified external file", "overwrite": True,
            }), tool_call_id="write-external")}

    resources.runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=registry.build_local_tools(policy, default_timeout=5, parallel_mode="sequential"),
        toolsets=[], instructions="test", limits=LimitsConfig(request_count=4), tool_metadata={},
        capability_gateway=gateway, work_completion_issues=work.completion_blockers,
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        full = (await host.dispatch(CreateSession())).session_id
        other = (await host.dispatch(CreateSession())).session_id
        with pytest.raises(InvalidStateError, match="明确确认"):
            await host.dispatch(SetPermissionPreset(full, "full_access"))
        assert (await host.snapshot(full)).sandbox_mode == "workspace_write"
        await host.dispatch(SetApprovalMode(other, "auto"))
        denied = await host.dispatch(StartRun(other, "write", "denied"))
        await asyncio.wait_for(_collect_run(host, denied.run_id), timeout=5)
        assert not target.exists()
        await host.dispatch(SetPermissionPreset(full, "full_access", confirmed=True))
        assert (await host.snapshot(full)).approval_mode == "auto"
        assert (await host.snapshot(full)).sandbox_mode == "disabled"
        phase = 0
        started = await host.dispatch(StartRun(full, "write", "allowed"))
        await asyncio.wait_for(_collect_run(host, started.run_id), timeout=5)
        assert target.read_text() == "verified external file"
        assert work.state_for(full).effects[-1].status.value == "verified"
        fork = resources.session_repository.fork(full, through_turn=0)
        assert resources.session_repository.load(fork.id).settings.sandbox_mode == "workspace_write"
        assert (await host.snapshot(other)).sandbox_mode == "workspace_write"
        with pytest.raises(WorkspaceViolation):
            work.workspace.resolve(target)
        await host.dispatch(SetPermissionPreset(full, "workspace_auto"))
        assert (await host.snapshot(full)).sandbox_mode == "workspace_write"
        settings = resources.session_repository.load(full).settings
        assert settings.sandbox_mode == "workspace_write"
    finally:
        await host.close()


async def test_permission_change_is_rejected_while_execution_is_running(tmp_path: Path) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        yield "done"

    runtime = AgentRuntime(model=FunctionModel(stream_function=stream), tools=[], toolsets=[],
                           instructions="test", limits=LimitsConfig(), tool_metadata={})
    resources = LocalResources(tmp_path, runtime)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = (await host.dispatch(CreateSession())).session_id
        run = await host.dispatch(StartRun(session, "wait", "wait"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(WorkspaceBusyError):
            await host.dispatch(SetPermissionPreset(session, "full_access", confirmed=True))
        assert resources.session_repository.load(session).settings.sandbox_mode is None
        release.set()
        await _collect_run(host, run.run_id)
    finally:
        release.set()
        await host.close()


def test_permission_api_requires_unambiguous_confirmed_preset_and_restores_saved_scope(
    tmp_path: Path,
) -> None:
    resources = ApiResources(tmp_path)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch", api_only=True)
    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch"})
        headers = {"Origin": "http://testserver"}
        session = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        path = f"/api/v1/sessions/{session}/settings"
        for body in (
            {"permissionPreset": "full_access"},
            {"permissionPreset": "full_access", "confirmFullAccess": "true"},
            {"permissionPreset": "workspace_auto", "approvalMode": "manual"},
            {"permissionPreset": "full_access", "confirmFullAccess": True, "reasoningEffort": "low"},
        ):
            assert client.patch(path, headers=headers, json=body).status_code == 422
        result = client.patch(path, headers=headers, json={
            "permissionPreset": "full_access", "confirmFullAccess": True,
        })
        assert result.status_code == 200
        assert result.json()["sandbox_mode"] == "disabled"
        assert client.get(f"/api/v1/sessions/{session}").json()["sandboxMode"] == "disabled"
        assert client.get("/api/v1/bootstrap").json()["sandboxMode"] == "workspace_write"
    restored = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    with TestClient(create_web_app(restored, launch_token="again", api_only=True),
                    base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "again"})
        snapshot = client.get(f"/api/v1/sessions/{session}").json()
        assert snapshot["sandboxMode"] == "disabled"
        assert snapshot["approvalMode"] == "auto"


async def test_sandbox_scope_is_task_local_and_does_not_relax_fixed_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lumen.sandbox.platform.system", lambda: "unsupported")
    workspace = Workspace(tmp_path, use_session_permissions=True)
    fixed = Workspace(tmp_path)
    sandbox = SandboxRunner(tmp_path, SandboxConfig(
        extra_read_paths=[tmp_path.parent], extra_write_paths=[tmp_path.parent],
    ))

    async def check(mode: str) -> None:
        with sandbox_scope("disabled" if mode == "full" else "workspace_write"):
            await asyncio.sleep(0)
            assert workspace.allow_external_paths is (mode == "full")
            assert fixed.allow_external_paths is False
            if mode == "full":
                prepared = sandbox.prepare([sys.executable, "-V"], cwd=tmp_path.parent)
                try:
                    assert len(prepared.argv) == 2
                    assert prepared.argv[-1] == "-V"
                finally:
                    prepared.cleanup()
            else:
                assert not sandbox.config.extra_read_paths
                assert not sandbox.config.extra_write_paths
                with pytest.raises(SandboxUnavailableError):
                    sandbox.prepare([sys.executable, "-V"], cwd=tmp_path)

    await asyncio.gather(check("full"), check("workspace"))
    assert not workspace.allow_external_paths
    assert sandbox.config.mode == "workspace_write"
    assert sandbox.config.extra_write_paths == [tmp_path.parent]


async def test_other_host_does_not_restore_cached_full_access_when_changing_approval(tmp_path: Path) -> None:
    first = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    second = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await first.open()
    await second.open()
    try:
        session = (await first.dispatch(CreateSession())).session_id
        await first.dispatch(SetPermissionPreset(session, "full_access", confirmed=True))
        assert (await second.snapshot(session)).sandbox_mode == "disabled"
        await first.dispatch(SetPermissionPreset(session, "workspace_auto"))
        await second.dispatch(SetApprovalMode(session, "manual"))
        assert (await first.snapshot(session)).sandbox_mode == "workspace_write"
    finally:
        await first.close()
        await second.close()


async def test_configured_extra_directories_are_reported_without_claiming_workspace_only(
    tmp_path: Path,
) -> None:
    resources = LocalResources(tmp_path, _runtime())
    resources.config.sandbox = SandboxConfig(extra_write_paths=[tmp_path.parent])
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    session = (await host.dispatch(CreateSession())).session_id
    assert (await host.snapshot(session)).filesystem_scope == "configured"
    await host.dispatch(SetPermissionPreset(session, "workspace_auto"))
    assert (await host.snapshot(session)).filesystem_scope == "workspace"
    assert resources.config.sandbox.extra_write_paths == [tmp_path.parent]


@pytest.mark.parametrize(("spawn_mode", "current_mode"), [
    ("disabled", "workspace_write"), ("workspace_write", "disabled"), ("disabled", "disabled"),
])
async def test_readonly_child_uses_intersection_of_snapshot_and_current_parent_permissions(
    tmp_path: Path, spawn_mode: str, current_mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = manager(tmp_path)
    factory = resources.agent_runtime_factory
    factory.sandbox = SandboxConfig(mode="disabled")
    session = resources.session_repository.create(agent_name="test", model_id="test")
    profile = resources.agent_profiles["explorer"]
    with sandbox_scope("disabled" if spawn_mode == "disabled" else "workspace_write"):
        config = factory.snapshot(profile, approval_mode="auto")
    resources.session_repository.append_session_settings(session.id, SessionSettingsState(
        sandbox_mode="disabled" if current_mode == "disabled" else "workspace_write",
    ))
    thread = AgentThreadState(
        ref=AgentThreadRef(id="child", path="/root/child", parent_session_id=session.id,
                          root_run_id="run", agent_type="explorer"),
        task="inspect", task_name="inspect", config=config, idempotency_key="inspect",
    )

    async def execute(*_args: object) -> AgentExecutionResult:
        expected = "disabled" if spawn_mode == current_mode == "disabled" else "workspace_write"
        assert session_sandbox_mode.get() == expected
        return AgentExecutionResult(output="scope verified")

    monkeypatch.setattr(factory, "_execute_runtime", execute)
    await factory.execute(thread, profile, [])
