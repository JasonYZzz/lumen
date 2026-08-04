from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.application import (
    ApprovalAlreadyResolvedError,
    CancelRun,
    ContextControl,
    CreateSession,
    DecideApproval,
    EventEnvelope,
    InvokeSkill,
    StartRun,
    WorkspaceBusyError,
    WorkspaceHost,
)
from lumen.config import LimitsConfig
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository
from lumen.skills import Skill


class LocalResources:
    def __init__(self, root: Path, runtime: AgentRuntime) -> None:
        self.workspace = root
        self.session_repository = SessionRepository(root / "sessions")
        self.runtime = runtime
        self.config = SimpleNamespace(
            agent=SimpleNamespace(name="test-agent"),
            permissions=SimpleNamespace(default_mode="manual"),
        )
        self.tool_metadata: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []
        self.skills: list[object] = []

    async def open(self) -> LocalResources:
        return self

    async def close(self) -> None:
        return None

    def active_model_config(self) -> SimpleNamespace:
        return SimpleNamespace(id="test-model")

    def active_model_name(self) -> str:
        return "test"

    def available_models(self) -> list[str]:
        return ["test"]

    def mcp_summary(self) -> list[dict[str, object]]:
        return []

    def summary(self) -> dict[str, object]:
        return {"agent": "test-agent", "model": "test-model"}

    def load_skill_by_name(self, name: str) -> object | None:
        return next((skill for skill in self.skills if getattr(skill, "name", None) == name), None)


def _runtime(output: str = "web ready") -> AgentRuntime:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield output

    return AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )


async def test_workspace_host_runs_and_replays_a_session_through_its_public_interface(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(
            StartRun(
                session_id=created.session_id,
                input="hello web",
                client_request_id="request-1",
            )
        )

        events = [event async for event in host.subscribe(started.run_id)]
        snapshot = await host.snapshot(created.session_id)
    finally:
        await host.close()

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.type for event in events] == [
        "run.started",
        "assistant.delta",
        "usage.updated",
        "run.completed",
    ]
    assert events[1].data == {"text": "web ready"}
    assert snapshot.session_id == created.session_id
    assert snapshot.last_user_input == "hello web"
    assert snapshot.active_run_id is None
    assert snapshot.timeline[-1].kind.value == "assistant"
    assert snapshot.timeline[-1].text == "web ready"


async def test_workspace_host_deduplicates_requests_and_rejects_parallel_runs(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        yield "done"

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        first_session = await host.dispatch(CreateSession())
        second_session = await host.dispatch(CreateSession())
        command = StartRun(first_session.session_id, "one", "same-request")
        first = await host.dispatch(command)
        await asyncio.wait_for(entered.wait(), timeout=1)

        duplicate = await host.dispatch(command)
        with pytest.raises(WorkspaceBusyError):
            await host.dispatch(StartRun(second_session.session_id, "two", "request-2"))

        release.set()
        _ = [event async for event in host.subscribe(first.run_id)]
    finally:
        await host.close()

    assert duplicate.run_id == first.run_id


async def test_workspace_host_cancel_is_persisted_and_idempotent(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await asyncio.Event().wait()
        yield "unreachable"

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "wait", "cancel-request"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        cancelled = await host.dispatch(CancelRun(started.run_id))
        again = await host.dispatch(CancelRun(started.run_id))
        events = [event async for event in host.subscribe(started.run_id)]
        turn = resources.session_repository.load(session.session_id).turns[0]
    finally:
        await host.close()

    assert cancelled.status == "cancelled"
    assert again.status == "cancelled"
    assert events[-1].type == "run.cancelled"
    assert turn.status == "cancelled"


def _last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


async def test_workspace_host_manual_approval_can_be_resolved_from_another_request(
    tmp_path: Path,
) -> None:
    executed = False

    def write_note(content: str) -> str:
        nonlocal executed
        executed = True
        return content

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = _last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("write_note", '{"content":"safe"}', tool_call_id="call-web")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "plugin:test", "risk": "write"}},
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "write", "approval-request"))
        stream_events = host.subscribe(started.run_id)
        seen: list[EventEnvelope] = []
        async for event in stream_events:
            seen.append(event)
            if event.type == "approval.pending":
                assert event.data["presentation"]["title"] == "write_note · write · plugin:test"
                await host.dispatch(DecideApproval(started.run_id, "call-web", False))
                repeated = await host.dispatch(DecideApproval(started.run_id, "call-web", False))
                assert repeated.status == "denied"
                with pytest.raises(ApprovalAlreadyResolvedError):
                    await host.dispatch(DecideApproval(started.run_id, "call-web", True))
        turn = host.resources.session_repository.load(session.session_id).turns[0]
    finally:
        await host.close()

    assert executed is False
    assert any(event.type == "approval.resolved" and not event.data["approved"] for event in seen)
    assert turn.approvals[0]["approved"] is False


async def test_workspace_host_invokes_skills_and_routes_context_controls(tmp_path: Path) -> None:
    seen_prompts: list[str] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        seen_prompts.append(str(messages[-1]))
        yield "skill complete"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )

    class FakeContextEngine:
        async def control(self, command: object, emit: object) -> object:
            del emit
            return SimpleNamespace(
                status="ok",
                message=type(command).__name__,
                payload={"action": getattr(command, "action", "report")},
            )

    runtime.context_engine = FakeContextEngine()  # type: ignore[assignment]
    resources = LocalResources(tmp_path, runtime)
    skill_file = tmp_path / ".lumen" / "skills" / "review" / "SKILL.md"
    resources.skills = [
        Skill(
            name="review",
            description="Review code",
            file_path=skill_file,
            base_dir=skill_file.parent,
            body="Inspect the selected file carefully.",
            source="project",
        )
    ]
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        report = await host.dispatch(ContextControl(session.session_id, "report"))
        memory = await host.dispatch(ContextControl(session.session_id, "memory", action="list"))
        runtime.context_engine = None
        started = await host.dispatch(
            InvokeSkill(session.session_id, "review", "src/main.py", "skill-request")
        )
        _ = [event async for event in host.subscribe(started.run_id)]
        turn = resources.session_repository.load(session.session_id).turns[-1]
    finally:
        await host.close()

    assert report.data["message"] == "ContextReportCommand"
    assert memory.data["payload"] == {"action": "list"}
    assert turn.user_input == "/skill:review src/main.py"
    assert "Inspect the selected file carefully." in seen_prompts[-1]
    assert "src/main.py" in seen_prompts[-1]
