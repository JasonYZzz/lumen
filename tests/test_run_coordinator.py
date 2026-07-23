from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.config import LimitsConfig
from lumen.events import RunEvent, RunStarted
from lumen.plan import PlanState, PlanStep, StepStatus
from lumen.run_coordinator import RunCoordinator, RunInput
from lumen.runtime import (
    AgentRuntime,
    PartialRunOutcome,
    ToolApproval,
    attach_partial_outcome,
)
from lumen.sessions import SessionRepository


def _runtime(text: str = "done") -> AgentRuntime:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield text

    return AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )


async def test_coordinator_owns_session_run_and_resume_state(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    runtime = _runtime()
    coordinator = RunCoordinator(
        repository=repository,
        agent_name="test-agent",
        model_id=lambda: "test-model",
        runtime=lambda: runtime,
    )
    session = coordinator.new_session()
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await coordinator.run("hello", emit, approve)

    assert outcome is not None
    assert coordinator.state.session.id == session.session.id
    assert coordinator.state.history
    loaded = repository.load(session.session.id)
    assert loaded.turns[0].status == "completed"
    assert loaded.turns[0].user_input == "hello"

    resumed = coordinator.resume(session.session.id)
    assert resumed.history == loaded.history
    assert resumed.last_user_input == "hello"


async def test_coordinator_separates_display_input_from_model_prompt(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)
    runtime = _runtime()
    coordinator = RunCoordinator(
        repository=repository,
        agent_name="test-agent",
        model_id=lambda: "test-model",
        runtime=lambda: runtime,
    )
    session = coordinator.new_session().session
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    await coordinator.run(
        RunInput(display_text="review @README.md", model_prompt="review <file>contents</file>"),
        emit,
        approve,
    )

    assert isinstance(events[0], RunStarted)
    assert events[0].prompt == "review @README.md"
    assert repository.load(session.id).turns[0].user_input == "review @README.md"


async def test_failed_partial_plan_becomes_retry_and_resume_plan(tmp_path: Path) -> None:
    latest = PlanState(
        steps=[PlanStep(id="one", title="One", status=StepStatus.IN_PROGRESS)],
        revision=2,
    )

    class FailingRuntime:
        async def run(self, *_args: Any, **_kwargs: Any) -> None:
            error = RuntimeError("failed")
            raise attach_partial_outcome(
                error,
                PartialRunOutcome(status="failed", message="failed", plan=latest),
            )

    repository = SessionRepository(tmp_path)
    coordinator = RunCoordinator(
        repository=repository,
        agent_name="test-agent",
        model_id=lambda: "test-model",
        runtime=lambda: FailingRuntime(),  # type: ignore[arg-type]
    )
    session = coordinator.new_session().session

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    assert await coordinator.run(RunInput("task", "expanded task"), emit, approve) is None
    assert coordinator.state.plan == latest
    assert coordinator.state.history == []
    resumed = coordinator.resume(session.id)
    assert resumed.plan == latest
    assert resumed.last_user_input == "task"


async def test_coordinator_persists_failed_partial_outcome(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path)

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider exploded")
        yield "unreachable"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    coordinator = RunCoordinator(
        repository=repository,
        agent_name="test-agent",
        model_id=lambda: "test-model",
        runtime=lambda: runtime,
    )
    session = coordinator.new_session().session

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await coordinator.run("fail", emit, approve)

    assert outcome is None
    turn = repository.load(session.id).turns[0]
    assert turn.status == "failed"
    assert turn.error_message == "provider exploded"
    assert turn.retryable is False
    assert turn.usage is not None
