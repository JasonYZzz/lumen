from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from test_reasoning import manager

from lumen.agents.types import (
    AgentExecutionResult,
    AgentMessage,
    AgentProfile,
    AgentStatus,
    AgentThreadRef,
    AgentThreadState,
)
from lumen.config import SandboxConfig
from lumen.sandbox import SandboxRunner
from lumen.tools.capability import run_prepared_command


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
async def test_command_reaps_descendants_after_leader_exit(tmp_path: Path, cancel: bool) -> None:
    ready = tmp_path / "ready"
    escaped = tmp_path / "escaped"
    code = (
        "import os,signal,time,pathlib; pid=os.fork(); "
        "os._exit(0) if pid else None; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(1); "
        f"pathlib.Path({str(escaped)!r}).write_text('escaped')"
    )
    argv = [sys.executable, "-c", code]
    sandbox = SandboxRunner(tmp_path, SandboxConfig(mode="disabled"))
    task = asyncio.create_task(run_prepared_command(
        sandbox.prepare(argv, cwd=tmp_path), argv=argv, resolved_cwd=tmp_path, cwd=".",
        timeout_seconds=0.2 if not cancel else 3, sandbox_config=sandbox.config,
    ))
    async with asyncio.timeout(2):
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
