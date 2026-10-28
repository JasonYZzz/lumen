from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_runtime import _LumenRecoveringWriteDriver, last_tool_return  # pyright: ignore[reportPrivateUsage]
from test_workspace_host import LocalResources

from lumen.agent_loop.driver import (
    ModelDriverRequest,
    ModelResponseCompleted,
    ModelResponseStarted,
    ModelStopReason,
    ModelStreamEvent,
    ModelToolArgumentsDelta,
    ModelToolCallCompleted,
    ModelToolCallStarted,
)
from lumen.application import CreateSession, InvalidStateError, RetryRun, StartRun, WorkspaceHost
from lumen.config import LimitsConfig, PermissionsConfig
from lumen.context import ArtifactStore
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency, ToolSpec
from lumen.work_products import TaskWorkspace


def resources(root: Path, *, phase: str | None = None) -> LocalResources:
    async def unused(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        raise AssertionError("only the native Loop executes")
        yield "unreachable"

    async def write_note(content: str) -> dict[str, str]:
        counter = root / "counter"
        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
        if phase == "dispatch" or (phase == "parallel" and content == "pending"):
            print(json.dumps({"ready": phase}), flush=True)
            await asyncio.Event().wait()
        return {"written": content}

    class Driver(_LumenRecoveringWriteDriver):
        async def stream(self, request: ModelDriverRequest[ModelMessage]) -> AsyncIterator[ModelStreamEvent]:
            if phase == "parallel":
                yield ModelResponseStarted(sequence=0)
                for index, content in enumerate(("once", "pending"), 1):
                    yield ModelToolCallStarted(sequence=index * 2 - 1, call_id=f"parallel-{index}",
                        name="write_note")
                    yield ModelToolCallCompleted(sequence=index * 2, call_id=f"parallel-{index}",
                        name="write_note", arguments={"content": content})
                yield ModelResponseCompleted(sequence=5, stop_reason=ModelStopReason.TOOL_CALL)
                return
            if phase == "arguments":
                yield ModelResponseStarted(sequence=0)
                yield ModelToolCallStarted(sequence=1, call_id="incomplete", name="write_note")
                yield ModelToolArgumentsDelta(sequence=2, call_id="incomplete", arguments_delta='{"content":')
                print(json.dumps({"ready": phase}), flush=True)
                await asyncio.Event().wait()
            if phase == "request" or (phase == "checkpoint" and last_tool_return(request.messages)):
                print(json.dumps({"ready": phase}), flush=True)
                await asyncio.Event().wait()
            async for item in super().stream(request):
                yield item

    registry = ToolRegistry(root)
    registry.add(ToolSpec(write_note, risk=Risk.WRITE, effect_kind=EffectKind.EXTERNAL_ACTION,
        concurrency=lambda _: ToolConcurrency.PARALLEL_SAFE), origin="test")
    repository = SessionRepository(root / "sessions")
    original_append = repository.append_turn
    if phase == "post_success":
        def pause_before_checkpoint(session_id: str, **kwargs: object) -> None:
            if kwargs.get("checkpoint") is not None:
                print(json.dumps({"ready": phase}), flush=True)
                threading.Event().wait()
            original_append(session_id, **kwargs)  # type: ignore[arg-type]
        repository.append_turn = pause_before_checkpoint  # type: ignore[method-assign]
    artifacts = ArtifactStore(root / "artifacts")
    workspace = TaskWorkspace(root, artifacts, repository)
    gateway = CapabilityGateway(registry, PermissionPolicy(PermissionsConfig(default_mode="auto")),
        default_timeout=300, effect_recorder=workspace.record_tool_effect)
    driver = Driver()
    driver.fail_after_tool = False
    runtime = AgentRuntime(model=FunctionModel(stream_function=unused), tools=[], toolsets=[],
        instructions="Write once.", limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}}, model_driver=driver,
        capability_gateway=gateway)
    result = LocalResources(root, runtime, artifact_store=artifacts)
    result.config.permissions.default_mode = "auto"
    result.session_repository = repository
    result.task_workspace = workspace
    result.capability_gateway = gateway
    return result


async def child(root: Path, phase: str) -> None:
    host = WorkspaceHost(resources(root, phase=phase))  # type: ignore[arg-type]
    await host.open()
    created = await host.dispatch(CreateSession())
    (root / "session-id").write_text(created.session_id)
    started = await host.dispatch(StartRun(created.session_id, "write", "original"))
    async for event in host.subscribe(started.run_id):
        if event.type == "approval.pending":
            from lumen.application import DecideApproval
            await host.dispatch(DecideApproval(started.run_id, event.data["call_id"], True))
        if phase == "terminal" and event.type == "run.completed":
            print(json.dumps({"ready": phase}), flush=True)
            await asyncio.Event().wait()


@pytest.mark.parametrize(
    "phase", ["request", "arguments", "dispatch", "parallel", "checkpoint", "post_success", "terminal"],
)
async def test_hard_kill_recovers_only_durable_boundaries_without_duplicate_effects(
    tmp_path: Path, phase: str,
) -> None:
    process = await asyncio.create_subprocess_exec(sys.executable, "-u", __file__, str(tmp_path), phase,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert process.stdout is not None
        async with asyncio.timeout(20):
            marker = await process.stdout.readline()
    finally:
        if process.returncode is None:
            process.kill()
        _, error_output = await process.communicate()
    assert marker, error_output.decode()[:1000]
    assert json.loads(marker)["ready"] == phase
    session_id = (tmp_path / "session-id").read_text()
    loaded = SessionRepository(tmp_path / "sessions").load(session_id, revalidate=True)
    assert len(loaded.turns) == 1
    assert loaded.turns[0].status == ("completed" if phase == "terminal" else "running")
    assert bool(loaded.history) is (phase in {"checkpoint", "terminal"})
    assert bool(loaded.turns[0].checkpoint) is (phase == "checkpoint")
    if phase in {"request", "arguments"}:
        assert not (tmp_path / "counter").exists()
        return
    expected_count = "2" if phase == "parallel" else "1"
    assert (tmp_path / "counter").read_text() == expected_count
    if phase == "post_success":
        assert loaded.work_state.effects[0].recovery_receipt is not None
    host = WorkspaceHost(resources(tmp_path))  # type: ignore[arg-type]
    await host.open()
    try:
        if phase in {"dispatch", "parallel"}:
            with pytest.raises(InvalidStateError, match="resolve interrupted run effects"):
                await host.dispatch(RetryRun(session_id, "retry"))
        elif phase in {"checkpoint", "post_success"}:
            started = await host.dispatch(RetryRun(session_id, "retry"))
            events = [event async for event in host.subscribe(started.run_id)]
            assert events[-1].type == "run.completed"
        assert (tmp_path / "counter").read_text() == expected_count
    finally:
        await host.close()


async def test_interrupted_verified_effect_without_replay_evidence_blocks_retry(tmp_path: Path) -> None:
    resource = resources(tmp_path)
    session = resource.session_repository.create(agent_name="test", model_id="offline")
    workspace = resource.task_workspace
    assert workspace is not None
    workspace.bind_session(session.id)
    workspace.bind_interaction("interrupted")
    resource.session_repository.append_turn_started(
        session.id, user_input="write", interaction_id="interrupted",
    )
    effect = workspace.record_tool_effect(
        tool_name="write_note", effect_kind=EffectKind.EXTERNAL_ACTION,
        success=True, summary="succeeded before the checkpoint",
    )
    assert effect is not None and effect.recovery_receipt is None
    host = WorkspaceHost(resource)  # type: ignore[arg-type]
    await host.open()
    try:
        with pytest.raises(InvalidStateError, match="resolve interrupted run effects"):
            await host.dispatch(RetryRun(session.id, "retry"))
    finally:
        await host.close()


if __name__ == "__main__":
    asyncio.run(child(Path(sys.argv[1]), sys.argv[2]))
