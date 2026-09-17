from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from test_reasoning import manager

from lumen.agents.types import (
    AgentExecutionResult,
    AgentMessage,
    AgentProfile,
    AgentStatus,
    AgentThreadRef,
    AgentThreadState,
)
from lumen.config import ModelSettingsConfig, SandboxConfig
from lumen.runtime import ToolApproval
from lumen.sandbox import SandboxRunner
from lumen.tools.capability import run_prepared_command
from lumen.work_products import EffectStatus


def test_child_snapshot_excludes_host_owned_git_mutations(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    factory = resources.agent_runtime_factory
    git_metadata = {
        name: {"origin": "builtin", "risk": risk, "effect": effect}
        for name, risk, effect in (
            ("git_status", "read", "observe"),
            ("git_diff", "read", "observe"),
            ("git_stage", "write", "mutation"),
            ("git_commit", "confirm", "mutation"),
            ("git_push", "confirm", "external_action"),
        )
    }
    factory.parent_tool_metadata = lambda: git_metadata
    factory.enabled_builtins = tuple(git_metadata)

    snapshot = factory.snapshot(resources.agent_profiles["worker"], approval_mode="manual")

    assert snapshot.tool_names == ("git_diff", "git_status")


@pytest.mark.parametrize("status", [
    AgentStatus.FAILED, AgentStatus.WAITING, AgentStatus.RECONCILIATION_REQUIRED,
])
async def test_unfinished_worktree_execution_is_not_marked_completed_or_committed(
    tmp_path: Path, status: AgentStatus,
) -> None:
    resources = manager(tmp_path)
    factory = resources.agent_runtime_factory
    profile = resources.agent_profiles["worker"]
    session = resources.session_repository.create(agent_name="test", model_id="test")
    thread = AgentThreadState(
        ref=AgentThreadRef(id="agent-failed", path="/root/failed", parent_session_id=session.id,
                          root_run_id="run", agent_type="worker"),
        task="execute", task_name="failed", config=factory.snapshot(profile, approval_mode="manual"),
        idempotency_key="failed", worktree=str(tmp_path), base_commit="baseline",
    )

    async def git(cwd: Path, *args: str) -> str:
        # No status/stage/commit is allowed after an unfinished execution.
        assert args == ("rev-parse", "--show-toplevel")
        assert cwd == factory.workspace
        return str(tmp_path)

    async def execute(
        thread: AgentThreadState, profile: AgentProfile, messages: Sequence[AgentMessage], cwd: Path,
    ) -> AgentExecutionResult:
        del thread, profile, messages, cwd
        return AgentExecutionResult(status=status, output="unfinished", error="requires recovery")

    factory._git = git  # pyright: ignore[reportPrivateUsage]
    factory._execute_runtime = execute  # pyright: ignore[reportPrivateUsage]
    result = await factory.execute(thread, profile, [])
    assert result.status is status
    assert result.commit is None
    assert result.worktree == str(tmp_path)
    assert result.error == "requires recovery"


@pytest.mark.parametrize("fail", [False, True])
async def test_child_execution_receipt_updates_prepared_identity_and_preserves_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool,
) -> None:
    resources = manager(tmp_path)
    factory = resources.agent_runtime_factory
    profile = resources.agent_profiles["worker"]
    session = resources.session_repository.create(agent_name="test", model_id="test")
    config = factory.snapshot(profile, approval_mode="manual").model_copy(
        update={"tool_names": ("run_command",)}
    )
    thread = AgentThreadState(
        ref=AgentThreadRef(id="agent-effect", path="/root/effect", parent_session_id=session.id,
                          root_run_id="run", agent_type="worker"),
        task="execute", task_name="effect", config=config, idempotency_key="effect",
    )
    calls = 0

    async def run_command(argv: list[str]) -> str:
        assert argv == ["example"]
        if fail:
            raise RuntimeError("failed after dispatch")
        return "executed"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: DeltaToolCall("run_command", '{"argv":["example"]}', tool_call_id="execute")}
        else:
            yield "done"

    async def approve(_request: object) -> ToolApproval:
        return ToolApproval(True, "test approval")

    metadata = {"run_command": {"origin": "builtin", "risk": "execute", "effect": "execution"}}

    def build_model(_config: ModelSettingsConfig) -> FunctionModel:
        return FunctionModel(stream_function=stream)

    def tools_for(
        _thread: AgentThreadState, _cwd: Path,
    ) -> tuple[list[Tool[None]], dict[str, dict[str, str]]]:
        return [Tool[None](run_command)], metadata

    monkeypatch.setattr("lumen.agents.runtime_factory.build_model", build_model)
    monkeypatch.setattr(factory, "_tools_for", tools_for)
    factory.bind_approval_handler(approve)
    factory.bind_progress_handler(None)
    factory.bind_status_handler(None)
    result = await factory._execute_runtime(thread, profile, [], tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert result.status is (AgentStatus.RECONCILIATION_REQUIRED if fail else AgentStatus.COMPLETED)
    assert len(result.effect_receipts) == 1
    receipt = result.effect_receipts[0]
    assert receipt.id == "effect:agent:agent-effect:1"
    assert receipt.status is (EffectStatus.RECONCILIATION_REQUIRED if fail else EffectStatus.VERIFIED)


@pytest.mark.parametrize("resolved", [False, True])
async def test_async_writable_recovery_holds_completion_and_preserves_user_resolution(
    tmp_path: Path, resolved: bool,
) -> None:
    resources = manager(tmp_path)
    factory = resources.agent_runtime_factory
    repository = resources.session_repository
    session = repository.create(agent_name="test", model_id="test")
    profile = resources.agent_profiles["worker"]
    thread = AgentThreadState(
        ref=AgentThreadRef(id="agent-recover", path="/root/recover", parent_session_id=session.id,
                           root_run_id="old", agent_type="worker"),
        task="recover", task_name="recover", status=AgentStatus.RUNNING,
        config=factory.snapshot(profile, approval_mode="manual"), idempotency_key="recover",
    )
    repository.append_agent_thread(session.id, thread)
    entered = asyncio.Event()
    release = asyncio.Event()
    inspected = asyncio.Event()

    async def classify(thread: AgentThreadState) -> AgentStatus:
        assert thread.ref.id == "agent-recover"
        entered.set()
        await release.wait()
        inspected.set()
        return AgentStatus.IMPORT_PENDING

    factory.classify_recovery = classify
    orchestrator = resources.agent_orchestrator
    try:
        orchestrator.bind_root_run(session.id, "new", approval_mode="manual")
        pending = repository.load(session.id).agent_state.threads[0]
        assert pending.status is AgentStatus.RECONCILIATION_REQUIRED
        await asyncio.wait_for(entered.wait(), 1)
        if resolved:
            repository.append_agent_thread(session.id, thread.model_copy(update={
                "status": AgentStatus.CLOSED, "resolution": "abandoned", "resolution_reason": "user decision",
            }))
        release.set()
        await asyncio.wait_for(inspected.wait(), 1)
        restored = repository.load(session.id).agent_state.threads[0]
        assert restored.status is (AgentStatus.CLOSED if resolved else AgentStatus.IMPORT_PENDING)
    finally:
        await orchestrator.shutdown()


async def test_worktree_execution_preserves_parent_and_rejects_dirty_import(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    resources.config.agents.worktree_root = tmp_path / "worktrees"
    factory = resources.agent_runtime_factory
    sandbox = SandboxRunner(tmp_path, SandboxConfig(mode="disabled"))

    async def git(*args: str) -> None:
        argv = ["git", *args]
        result = await run_prepared_command(sandbox.prepare(argv, cwd=tmp_path), argv=argv,
                                           resolved_cwd=tmp_path, cwd=".", timeout_seconds=3,
                                           sandbox_config=sandbox.config)
        assert result["exit_code"] == 0, result["stderr"]

    await git("init", "-q")
    (tmp_path / "tracked.txt").write_text("base\n")
    await git("add", "tracked.txt")
    await git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    profile = resources.agent_profiles["worker"]
    snapshot = factory.snapshot(profile, approval_mode="manual")
    thread = AgentThreadState(ref=AgentThreadRef(id="agent-test", path="/root/test", parent_session_id="test",
                                               root_run_id="test", agent_type="worker"),
                              task="edit", task_name="test", config=snapshot, idempotency_key="test")

    async def execute(
        thread: AgentThreadState, profile: AgentProfile, messages: Sequence[AgentMessage], cwd: Path,
    ) -> AgentExecutionResult:
        assert cwd != tmp_path
        await asyncio.to_thread((cwd / "tracked.txt").write_text, "child\n")
        return AgentExecutionResult(output="changed")

    factory._execute_runtime = execute  # pyright: ignore[reportPrivateUsage]
    result = await factory.execute(thread, profile, [])
    assert result.status is AgentStatus.IMPORT_PENDING
    assert result.usage["worktree_prepare_seconds"] >= 0
    assert result.usage["worktree_finalize_seconds"] >= 0
    assert (tmp_path / "tracked.txt").read_text() == "base\n"
    (tmp_path / "tracked.txt").write_text("user edit\n")
    completed = thread.model_copy(update={"commit": result.commit, "worktree": result.worktree,
                                          "base_commit": result.base_commit, "branch": result.branch})
    refused = await factory.import_changes(completed)
    assert refused.status is AgentStatus.RECONCILIATION_REQUIRED
    assert refused.usage["worktree_import_seconds"] >= 0
    assert (tmp_path / "tracked.txt").read_text() == "user edit\n"
    await factory.close(completed)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="process groups require POSIX")
@pytest.mark.parametrize("cancel", [False, True])
async def test_command_reaps_descendants_after_leader_exit(
    tmp_path: Path, cancel: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = tmp_path / "ready"
    escaped = tmp_path / "escaped"
    code = (
        "import os,signal,time,pathlib; pid=os.fork(); "
        "os._exit(0) if pid else None; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(1); "
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')"
    )
    argv = [sys.executable, "-c", code]
    real_spawn = asyncio.create_subprocess_exec

    async def spawn_ready(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await real_spawn(*argv, **kwargs)
        # The 0.2s timeout exercises cleanup, not interpreter/import startup.
        # Begin it only after the signal-ignoring descendant is ready.
        async with asyncio.timeout(10):
            while not ready.exists():  # noqa: ASYNC110 - external process readiness
                await asyncio.sleep(0.01)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_ready)
    sandbox = SandboxRunner(tmp_path, SandboxConfig(mode="disabled"))
    task = asyncio.create_task(run_prepared_command(
        sandbox.prepare(argv, cwd=tmp_path), argv=argv, resolved_cwd=tmp_path, cwd=".",
        timeout_seconds=0.2 if not cancel else 3, sandbox_config=sandbox.config,
    ))
    async with asyncio.timeout(10):
        while not ready.exists():  # noqa: ASYNC110 - an external process cannot signal an asyncio.Event
            await asyncio.sleep(0.01)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        assert (await asyncio.wait_for(task, 2))["timed_out"]
    await asyncio.sleep(1)
    assert not escaped.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="fixture uses a POSIX shebang executable")
async def test_cancelling_worktree_git_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = manager(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    ready = tmp_path / "git-ready"
    escaped = tmp_path / "escaped"
    binary = binary_dir / "git"
    binary.write_text(
        f"#!{sys.executable}\nimport pathlib,time\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\ntime.sleep(1)\n"
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    factory = resources.agent_runtime_factory
    task = asyncio.create_task(factory.parent_dirty_hash(tmp_path))
    async with asyncio.timeout(2):
        while not ready.exists():  # noqa: ASYNC110 - external executable readiness
            await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.sleep(1)
    assert not escaped.exists()
