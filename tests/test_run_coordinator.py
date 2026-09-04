from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import ContextConfig, LimitsConfig
from lumen.context import ContextEngine
from lumen.events import RunCompleted, RunEvent, RunFailed, RunStarted
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
    durable_terminal_statuses: list[str] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)
        if isinstance(event, RunCompleted):
            durable_terminal_statuses.append(repository.load(session.session.id).turns[-1].status)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await coordinator.run("hello", emit, approve)

    assert outcome is not None
    assert coordinator.state.session.id == session.session.id
    assert coordinator.state.history
    loaded = repository.load(session.session.id)
    assert loaded.turns[0].status == "completed"
    assert durable_terminal_statuses == ["completed"]
    assert isinstance(events[-1], RunCompleted)
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


@pytest.mark.parametrize("automatic", [False, True])
async def test_completed_write_survives_stream_failure_and_session_resume(
    tmp_path: Path, automatic: bool,
) -> None:
    executions = 0
    attempts = 0

    def write_note(content: str) -> str:
        """Write one note."""
        nonlocal executions
        executions += 1
        return content

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        has_result = any(
            part.part_kind == "tool-return" for message in messages for part in message.parts
        )
        if not has_result:
            yield {0: DeltaToolCall("write_note", '{"content":"once"}', tool_call_id="write-1")}
        elif attempts == 2:
            yield "discarded draft"
            if automatic:
                raise ModelHTTPError(503, "test", {"error": "temporarily unavailable"})
            raise RuntimeError("provider failed after write")
        else:
            yield "finished from saved tool result"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[Tool(write_note, sequential=True, requires_approval=True)], toolsets=[],
        instructions="write once", limits=LimitsConfig(model_retry_delay_seconds=0),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )
    repository = SessionRepository(tmp_path)
    coordinator = RunCoordinator(
        repository=repository, agent_name="agent", model_id=lambda: "test", runtime=lambda: runtime,
    )
    session = coordinator.new_session().session
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await coordinator.run("write once", emit, approve)
    if not automatic:
        assert outcome is None
        loaded = repository.load(session.id)
        assert loaded.turns[-1].status == "failed"
        assert len(loaded.full_history) == 3
        assert loaded.history == coordinator.state.history
        coordinator.resume(session.id)
        outcome = await coordinator.run("continue", emit, approve)
    assert outcome is not None
    assert outcome.output == "finished from saved tool result"
    assert executions == 1
    assert attempts == 3
    if automatic:
        assert not any(isinstance(event, RunFailed) for event in events)
        assert outcome.usage["model_attempts"] == 3


async def test_jsonl_failure_does_not_publish_completed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path)
    runtime = _runtime()
    coordinator = RunCoordinator(
        repository=repository,
        agent_name="test-agent",
        model_id=lambda: "test-model",
        runtime=lambda: runtime,
    )
    coordinator.new_session()
    before = coordinator.state

    def fail_append(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repository, "append_turn", fail_append)

    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    with pytest.raises(OSError, match="disk full"):
        await coordinator.run("hello", emit, approve)

    assert coordinator.state is before
    assert coordinator.state.history == []
    assert coordinator.state.full_history == []
    assert not any(isinstance(event, RunCompleted) for event in events)
    assert isinstance(events[-1], RunFailed)


async def test_jsonl_failure_does_not_publish_context_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "done"

    def summarize(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        return ModelResponse(
            parts=[
                TextPart(
                    content=(
                        '{"goals":["continue"],"constraints":[],"completed":[],'
                        '"current_plan":[],"important_files":[],"key_facts":[],'
                        '"failures_and_approvals":[],"outstanding":[]}'
                    )
                )
            ]
        )

    engine = ContextEngine(
        ContextConfig(
            enabled=True,
            soft_token_limit=10,
            keep_recent_tokens=2_000,
            background_compaction=False,
        ),
        model=FunctionModel(function=summarize),
    )
    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
        context_engine=engine,
    )
    repository = SessionRepository(tmp_path)
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

    assert await coordinator.run("first turn", emit, approve) is not None
    assert engine._last_checkpoints == {}  # type: ignore[reportPrivateUsage]

    def fail_append(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    original_append = repository.append_turn
    monkeypatch.setattr(repository, "append_turn", fail_append)
    with pytest.raises(OSError, match="disk full"):
        await coordinator.run("second turn", emit, approve)

    assert engine._last_checkpoints == {}  # type: ignore[reportPrivateUsage]
    assert len(repository.load(session.id).turns) == 1

    monkeypatch.setattr(repository, "append_turn", original_append)
    assert await coordinator.run("second turn", emit, approve) is not None
    assert len(repository.load(session.id).turns) == 2
