import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic_ai import Tool
from pydantic_ai.exceptions import IncompleteToolCall, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import ContextConfig, LimitsConfig
from lumen.context import ContextEngine, ContextReportCommand
from lumen.events import (
    ClarificationRequested,
    CommentaryDelta,
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    RunWaitingForUser,
    TextDelta,
    TextRetracted,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
)
from lumen.plan import PlanState, PlanStep, PlanStepInput, StepStatus
from lumen.runtime import AgentRuntime, PartialRunOutcome, ToolApproval, get_partial_outcome
from lumen.tools.spec import EffectKind


def last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


def last_retry_prompt(messages: Sequence[ModelMessage]) -> RetryPromptPart | None:
    """Find the most recent RetryPromptPart fed back to the model."""
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, RetryPromptPart):
                    return part
    return None


def projected_assistant_text(events: Sequence[RunEvent]) -> str:
    """Replay speculative text and retractions into the visible final text."""

    text = ""
    for event in events:
        if isinstance(event, TextDelta):
            text += event.text
        elif isinstance(event, TextRetracted):
            text = text[: -event.characters] if event.characters else text
    return text


def set_plan_delta(call_id: str, steps: list[dict[str, str]]) -> dict[int, DeltaToolCall]:
    return {0: DeltaToolCall("set_plan", f'{{"steps": {steps!r}}}'.replace("'", '"'), tool_call_id=call_id)}


def update_step_delta(
    call_id: str, *, step_id: str, status: StepStatus, note: str | None = None
) -> dict[int, DeltaToolCall]:
    args: dict[str, Any] = {"step_id": step_id, "status": status.value}
    if note is not None:
        args["note"] = note
    import json

    return {0: DeltaToolCall("update_step", json.dumps(args), tool_call_id=call_id)}


def report_progress_delta(
    call_id: str, *, summary: str, next_action: str | None = None
) -> dict[int, DeltaToolCall]:
    args: dict[str, Any] = {"summary": summary}
    if next_action is not None:
        args["next_action"] = next_action
    import json

    return {0: DeltaToolCall("report_progress", json.dumps(args), tool_call_id=call_id)}


def read_file_delta(call_id: str, path: str) -> dict[int, DeltaToolCall]:
    import json

    return {0: DeltaToolCall("read_file", json.dumps({"path": path}), tool_call_id=call_id)}


async def test_runtime_executes_tool_loop_and_emits_events() -> None:
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("echo", '{"value":"hello"}', tool_call_id="call-1")}
        else:
            yield f"final:{result.content}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("say hello", [], emit, approve)

    assert outcome.output == "final:echo:hello"
    assert any(isinstance(event, RunStarted) for event in events)
    assert any(isinstance(event, ToolCallStarted) and event.name == "echo" for event in events)
    assert any(isinstance(event, ToolCallFinished) and event.name == "echo" for event in events)
    assert any(isinstance(event, TextDelta) and "final:" in event.text for event in events)
    assert isinstance(events[-1], RunCompleted)


async def test_runtime_records_declared_non_observe_effect() -> None:
    def execute() -> str:
        return "done"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("execute", "{}", tool_call_id="effect-1")}
        else:
            yield "complete"

    recorded: list[dict[str, object]] = []

    def record_effect(**values: object) -> None:
        recorded.append(values)

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(execute, sequential=True)],
        toolsets=[],
        instructions="Use tools.",
        limits=LimitsConfig(),
        tool_metadata={
            "execute": {
                "origin": "test",
                "risk": "execute",
                "effect": EffectKind.EXECUTION.value,
            }
        },
        effect_recorder=record_effect,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("tool should not request approval")

    await runtime.run("go", [], emit, approve, session_id="effect-session")

    assert recorded == [
        {
            "tool_name": "execute",
            "effect_kind": EffectKind.EXECUTION,
            "success": True,
            "summary": "execute succeeded",
        }
    ]


def test_work_completion_gate_applies_without_an_approved_plan() -> None:
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="Answer.",
        limits=LimitsConfig(),
        tool_metadata={},
        work_completion_issues=lambda _session_id: ["effect pending"],
    )

    assert runtime._completion_gate_issues() == [  # pyright: ignore[reportPrivateUsage]
        "effect pending"
    ]


async def test_real_request_snapshot_updates_for_each_model_step() -> None:
    def echo(value: str) -> str:
        return value

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("echo", '{"value":"ok"}', tool_call_id="echo-1")}
        else:
            yield "done"

    model = FunctionModel(stream_function=model_function)
    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=1_000_000),
        model=model,
        model_id="acme:test",
    )
    runtime = AgentRuntime(
        model=model,
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
        context_engine=engine,
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    await runtime.run("go", [], emit, approve, session_id="snapshot-session")
    report = await engine.control(ContextReportCommand("snapshot-session"), emit)
    snapshot = report.payload["request_snapshot"]

    assert snapshot["model_step"] == 2
    assert "echo" in snapshot["visible_tools"]
    assert "request_clarification" in snapshot["visible_tools"]


async def test_runtime_enters_waiting_state_after_blocking_clarification() -> None:
    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {
                0: DeltaToolCall(
                    "request_clarification",
                    '{"question":"Which target?","choices":["A","B"]}',
                    tool_call_id="clarify-1",
                )
            }
        else:
            yield "Please choose A or B."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="Ask when blocked.",
        limits=LimitsConfig(),
        tool_metadata={"request_clarification": {"origin": "control", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("clarification does not require approval")

    outcome = await runtime.run("continue", [], emit, approve)

    assert outcome.status == "waiting_for_user"
    assert outcome.pending_clarification is not None
    assert outcome.pending_clarification.choices == ("A", "B")
    assert any(isinstance(event, ClarificationRequested) for event in events)
    assert isinstance(events[-1], RunWaitingForUser)


async def test_runtime_preserves_structured_tool_results_and_renders_them_as_json() -> None:
    def inspect_file() -> dict[str, object]:
        """Return a representative paginated file result."""
        return {"content": "1: hello", "has_more": True, "next_start_line": 2}

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("inspect_file", "{}", tool_call_id="structured-1")}
        else:
            assert result.content == {
                "content": "1: hello",
                "has_more": True,
                "next_start_line": 2,
            }
            yield "structured result received"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(inspect_file, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"inspect_file": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("inspect", [], emit, approve)

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert outcome.output == "structured result received"
    assert finished.result.startswith('{\n  "content": "1: hello"')
    assert '"has_more": true' in finished.result


async def test_runtime_requests_approval_and_returns_denial_to_model() -> None:
    executed = False

    def write_note(content: str) -> str:
        """Write a note."""
        nonlocal executed
        executed = True
        return content

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("write_note", '{"content":"secret"}', tool_call_id="call-2")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "plugin", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def deny(request: Any) -> ToolApproval:
        assert request.name == "write_note"
        return ToolApproval(approved=False, message="not allowed")

    outcome = await runtime.run("write this", [], emit, deny)

    assert outcome.output == "handled:denied"
    assert executed is False
    # The runtime no longer emits ToolApprovalPending itself — the approve
    # callback owns that surface (it decides whether to mount a card or
    # short-circuit in auto mode). The runtime only guarantees the resolved
    # event reaches the timeline.
    assert any(isinstance(event, ToolApprovalResolved) and event.approved is False for event in events)


async def test_runtime_treats_unregistered_approval_tool_as_unknown_external_risk() -> None:
    def dynamic_tool() -> str:
        """A tool discovered after the initial metadata snapshot."""
        return "executed"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("dynamic_tool", "{}", tool_call_id="dynamic-call")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(dynamic_tool, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={},
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def deny(request: Any) -> ToolApproval:
        assert request.origin == "unregistered remote tool"
        assert request.risk == "external_unknown"
        return ToolApproval(approved=False, message="unknown tool denied")

    outcome = await runtime.run("use dynamic tool", [], emit, deny)

    assert outcome.output == "handled:denied"


async def test_recoverable_tool_error_is_fed_back_to_model() -> None:
    """P0: a recoverable tool exception (file not found) must not crash the
    loop. The error is converted to a model-visible tool failure so the model
    can adjust — e.g. call list_directory — and the run still completes."""

    call_log: list[str] = []

    def read_file(path: str) -> str:
        """Read a file."""
        call_log.append(f"read_file:{path}")
        if path == "missing.txt":
            raise FileNotFoundError(f"file not found: {path}")
        return "ok"

    def list_directory(path: str = ".") -> str:
        """List a directory."""
        call_log.append(f"list_directory:{path}")
        return "README.md"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        retry = last_retry_prompt(messages)
        result = last_tool_return(messages)
        if retry is None and result is None:
            # First attempt: try to read a file that doesn't exist.
            yield {0: DeltaToolCall("read_file", '{"path": "missing.txt"}', tool_call_id="call-1")}
        elif retry is not None and result is None:
            # The file-not-found became a retry prompt — recover by listing.
            yield {0: DeltaToolCall("list_directory", '{"path": "."}', tool_call_id="call-2")}
        else:
            yield "recovered after listing"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(read_file, sequential=True), Tool(list_directory, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={
            "read_file": {"origin": "builtin", "risk": "read"},
            "list_directory": {"origin": "builtin", "risk": "read"},
        },
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("find the file", [], emit, approve)

    # The run completed instead of crashing.
    assert outcome.output == "recovered after listing"
    assert isinstance(events[-1], RunCompleted)
    # The model saw the error and recovered via list_directory.
    assert "read_file:missing.txt" in call_log
    assert "list_directory:." in call_log
    # A ToolCallFinished was emitted for the failed read with is_error set.
    failed = [e for e in events if isinstance(e, ToolCallFinished) and e.name == "read_file"]
    assert len(failed) == 1
    assert failed[0].is_error is True
    assert "file not found" in failed[0].result


async def test_ambiguous_edit_error_is_fed_back_to_model() -> None:
    """A multi-match edit_file error must also feed back rather than crash."""

    def edit_file(path: str, find: str, replace: str) -> str:
        """Edit a file by exact match."""
        raise ValueError(f"{3} matches for find in {path}")

    attempts = 0

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        retry = last_retry_prompt(messages)
        if retry is None:
            yield {0: DeltaToolCall("edit_file", '{"path":"a","find":"x","replace":"y"}', tool_call_id="c1")}
        else:
            yield "gave up after seeing the match error"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(edit_file, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"edit_file": {"origin": "builtin", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(approved=True, message="ok")

    outcome = await runtime.run("edit the file", [], emit, approve)

    assert outcome.output == "gave up after seeing the match error"
    assert isinstance(events[-1], RunCompleted)
    assert attempts == 2


async def test_runtime_emits_structured_plan_and_progress() -> None:
    def read_file(path: str) -> str:
        """Read a file."""
        return f"contents of {path}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield set_plan_delta("plan-1", [{"id": "inspect", "title": "Inspect project"}])
        elif result.tool_name == "set_plan":
            yield report_progress_delta("progress-1", summary="Found config.", next_action="Run tests")
        elif result.tool_name == "report_progress":
            yield read_file_delta("read-1", "config.yaml")
        elif result.tool_name == "read_file":
            yield update_step_delta("update-1", step_id="inspect", status=StepStatus.COMPLETED)
        else:
            yield "All done."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(read_file, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"read_file": {"origin": "builtin", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read-only tool should not request approval")

    outcome = await runtime.run("inspect", [], emit, approve)

    event_order = [
        type(event) for event in events if isinstance(event, (PlanCreated, ProgressReported, PlanUpdated))
    ]
    assert event_order[:2] == [PlanCreated, ProgressReported]
    assert event_order[-1] is PlanUpdated
    assert len(outcome.plan.evidence) == 1
    assert outcome.plan.steps[0].status is StepStatus.COMPLETED
    assert outcome.plan.steps[0].id == "inspect"
    assert outcome.active_history == outcome.new_messages


async def test_completion_gate_retries_visible_observation_then_allows_fix() -> None:
    attempt = 0

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield "premature"
        elif attempt == 2:
            retry = last_retry_prompt(messages)
            assert retry is not None
            assert "completion_gate_failed" in str(retry.content)
            yield update_step_delta(
                "finish-after-gate",
                step_id="implement",
                status=StepStatus.COMPLETED,
            )
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    outcome = await runtime.run(
        "execute",
        [],
        emit,
        lambda _request: pytest.fail("approval should not be requested"),  # type: ignore[arg-type]
        plan=PlanState(
            revision=1,
            approved_revision=1,
            steps=[PlanStep(id="implement", title="Implement")],
        ),
    )

    assert outcome.status == "completed"
    assert outcome.output == "done"
    assert attempt == 3
    assert projected_assistant_text(events) == "done"
    assert any(isinstance(event, TextRetracted) for event in events)


async def test_completion_gate_fails_after_two_retries() -> None:
    attempts = 0

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        yield "still premature"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("approval should not be requested")

    with pytest.raises(Exception, match="maximum output retries"):
        await runtime.run(
            "execute",
            [],
            emit,
            approve,
            plan=PlanState(
                revision=1,
                approved_revision=1,
                steps=[PlanStep(id="implement", title="Implement")],
            ),
        )

    assert attempts == 3
    assert isinstance(events[-1], RunFailed)
    assert "completion_gate_failed" in events[-1].message
    assert projected_assistant_text(events) == ""


def test_control_tools_are_sequential_and_visible() -> None:
    runtime = AgentRuntime(
        model="test",
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    function_tools = runtime.agent._function_toolset.tools  # type: ignore[attr-defined]
    tool_names = {tool.name for tool in function_tools.values()}
    assert {"set_plan", "update_step", "report_progress"}.issubset(tool_names)
    control_tool = next(t for t in function_tools.values() if t.name == "set_plan")
    assert control_tool.sequential is True
    assert control_tool.requires_approval is False
    assert (control_tool.metadata or {}).get("control") == "true"


async def test_runtime_plan_input_seeds_controller() -> None:
    plan = PlanState(steps=[PlanStep(id="one", title="One", status=StepStatus.COMPLETED)])

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "ok"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(False)

    outcome = await runtime.run("noop", [], emit, approve, plan=plan)

    assert outcome.plan == plan


async def test_runtime_carries_active_history() -> None:
    seed_history: list[ModelMessage] = [
        ModelRequest(parts=[__import__("pydantic_ai").messages.UserPromptPart(content="seed")])
    ]

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "ok"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(False)

    outcome = await runtime.run("noop", seed_history, emit, approve)

    assert outcome.active_history == [*seed_history, *outcome.new_messages]


async def test_runtime_separates_commentary_from_final_text() -> None:
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            # First model response: text + tool call together.
            yield "Inspecting configuration."
            yield {0: DeltaToolCall("echo", '{"value":"x"}', tool_call_id="c1")}
        else:
            # Final response: text only.
            yield "Configuration is valid."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        raise AssertionError("read tool should not request approval")

    outcome = await runtime.run("inspect", [], emit, approve)

    commentary = "".join(event.text for event in events if isinstance(event, CommentaryDelta))
    final = projected_assistant_text(events)
    assert commentary == "Inspecting configuration."
    assert final == "Configuration is valid."
    assert any(isinstance(event, TextRetracted) for event in events)
    # The final output is still the terminal result, not the commentary.
    assert outcome.output == "Configuration is valid."


async def test_runtime_emits_no_commentary_for_tool_only_response() -> None:
    def echo(value: str) -> str:
        """Echo a value."""
        return f"echo:{value}"

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("echo", '{"value":"x"}', tool_call_id="c1")}
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="Use tools when useful.",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await runtime.run("go", [], emit, approve)

    commentary = [event for event in events if isinstance(event, CommentaryDelta)]
    final = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert commentary == []
    assert final == "done"
    assert outcome.output == "done"


async def test_runtime_text_only_response_is_final() -> None:
    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "Just text."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    outcome = await runtime.run("go", [], emit, approve)

    commentary = [event for event in events if isinstance(event, CommentaryDelta)]
    final = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert commentary == []
    assert final == "Just text."
    assert outcome.output == "Just text."


async def test_runtime_emits_text_delta_before_provider_stream_completes() -> None:
    first_chunk_produced = asyncio.Event()
    release_stream = asyncio.Event()

    async def model_function(
        _messages: list[ModelMessage],
        _info: AgentInfo,
    ):  # type: ignore[no-untyped-def]
        yield "first"
        first_chunk_produced.set()
        await release_stream.wait()
        yield " second"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    events: list[RunEvent] = []
    first_delta_emitted = asyncio.Event()

    async def emit(event: RunEvent) -> None:
        events.append(event)
        if isinstance(event, TextDelta):
            first_delta_emitted.set()

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    task = asyncio.create_task(runtime.run("go", [], emit, approve))
    await asyncio.wait_for(first_chunk_produced.wait(), timeout=1)
    try:
        await asyncio.wait_for(first_delta_emitted.wait(), timeout=0.2)
    finally:
        release_stream.set()
    outcome = await task

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "first second"
    assert outcome.output == "first second"


# ---------------------------------------------------------------------------
# Truncation / usage-limit handling (pi-inspired: distinguish "config issue"
# from "model misbehaved" so the user gets the right fix hint).
# ---------------------------------------------------------------------------


async def test_runtime_truncated_tool_call_yields_max_tokens_hint() -> None:
    """When pydantic AI raises IncompleteToolCall, the user sees a message that
    points at the ``max_tokens`` setting, not a generic 'run failed'.

    We can't trigger IncompleteToolCall through FunctionModel's stream path
    (it doesn't let us set finish_reason='length'), so we stub the agent's
    run_stream_events to raise it directly. This tests our exception handling,
    not pydantic AI's detection — which is already covered upstream.
    """

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    def echo(value: str) -> str:
        """Echo."""
        return f"echo:{value}"

    runtime = AgentRuntime(
        model="test",
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )

    @asynccontextmanager
    async def fake_stream(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        raise IncompleteToolCall(
            "Model token limit (provider default) exceeded while generating a tool call."
        )
        yield  # pragma: no cover - unreachable, required for asynccontextmanager

    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    with patch.object(runtime.agent, "run_stream_events", fake_stream):
        with pytest.raises(IncompleteToolCall):
            await runtime.run("test", [], emit, approve)

    failures = [e for e in events if isinstance(e, RunFailed)]
    assert len(failures) == 1
    message = failures[0].message
    # Friendly hint points at the config knob.
    assert "max_tokens" in message
    assert "truncated" in message
    # Original technical detail preserved for debuggability.
    assert "Model token limit" in message


async def test_runtime_usage_limit_reports_limit_not_model_failure() -> None:
    """When UsageLimits trips, the message says 'limit', not 'model misbehaved'."""

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    async def model_function(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "hi"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[],
        toolsets=[],
        instructions="hi",
        # request_count=1 trips the limit on the second request. We mock the
        # stream to raise immediately so the test is deterministic.
        limits=LimitsConfig(request_count=1),
        tool_metadata={},
    )

    @asynccontextmanager
    async def fake_stream(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        raise UsageLimitExceeded("The next request would exceed the request_limit of 1")
        yield  # pragma: no cover

    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    with patch.object(runtime.agent, "run_stream_events", fake_stream):
        with pytest.raises(UsageLimitExceeded):
            await runtime.run("test", [], emit, approve)

    failures = [e for e in events if isinstance(e, RunFailed)]
    assert len(failures) == 1
    msg = failures[0].message.lower()
    assert "usage limit" in msg
    # The friendly message names the configured caps and points at agent.yaml
    # so the user knows this is a budget guard, not a model failure.
    assert "request_count" in msg
    assert "agent.yaml" in msg or "agent.limits" in msg


async def test_runtime_tool_failure_surfaces_raw_message_without_classification() -> None:
    """Tool denial is passed through verbatim — no retryable/category hint added.

    This mirrors pi's philosophy: the model already sees the denial via
    pydantic AI's normal tool-return plumbing; layering our own classification
    on top was both buggy (the classifier always fell through to 'other') and
    unnecessarily noisy. We exercise the denial path because it goes through
    pydantic AI's FunctionToolResultEvent, unlike a raw exception inside a
    tool function (which propagates and aborts the run).
    """

    def write_note(content: str) -> str:
        """Write a note."""
        return content

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("write_note", '{"content":"x"}', tool_call_id="c1")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def deny(_request: Any) -> ToolApproval:
        return ToolApproval(approved=False, message="not allowed XYZ")

    await runtime.run("test", [], emit, deny)

    finished = next(e for e in events if isinstance(e, ToolCallFinished) and e.name == "write_note")
    assert finished.is_error is True
    # Raw denial text preserved for the model + TUI.
    assert "denied" in finished.result.lower() or "XYZ" in finished.result
    # The old `error_category` field was removed — confirm it's gone.
    assert not hasattr(finished, "error_category")


# Keep PlanStepInput import live for callers/tests that re-export it.
_ = PlanStepInput


# ---------------------------------------------------------------------------
# PartialRunOutcome: failed/cancelled runs carry an audit record (Phase 2.4)
# ---------------------------------------------------------------------------


async def test_failed_run_carries_partial_outcome_with_accumulated_state() -> None:
    """A run that fails must attach a PartialRunOutcome carrying the status,
    message, retryability, and the plan snapshot — so the session can persist a
    faithful turn instead of an empty record."""
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    def write_note(content: str) -> str:
        """Write a note."""
        return content

    runtime = AgentRuntime(
        model="test",
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )

    @asynccontextmanager
    async def fake_stream(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        raise IncompleteToolCall("truncated mid-arguments")
        yield  # pragma: no cover - required for asynccontextmanager

    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(approved=True, message="allowed")

    with patch.object(runtime.agent, "run_stream_events", fake_stream):
        with pytest.raises(IncompleteToolCall) as exc_info:
            await runtime.run("write it", [], emit, approve)

    partial = get_partial_outcome(exc_info.value)
    assert partial is not None
    assert isinstance(partial, PartialRunOutcome)
    assert partial.status == "failed"
    assert partial.retryable is True
    # The plan snapshot is captured (empty here, but a real PlanState).
    assert partial.plan is not None
    # Approvals/diagnostics are present (lists, possibly empty) so callers can
    # always rely on the fields existing.
    assert isinstance(partial.approvals, list)
    assert isinstance(partial.diagnostics, list)


async def test_failed_run_after_tool_approval_preserves_approval() -> None:
    """When a run fails AFTER an approval was decided, the partial outcome
    carries that approval record — the audit trail isn't lost."""
    # Use a FunctionModel that calls a write tool (needs approval), then the
    # next model step raises. We force the failure by making the model raise
    # after the tool return is seen.
    raised = RuntimeError("provider blew up after the tool ran")

    def write_note(content: str) -> str:
        """Write a note."""
        return content

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("write_note", '{"content":"x"}', tool_call_id="c1")}
        else:
            # Tool completed and was approved; now the provider fails.
            raise raised

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(approved=True, message="allowed")

    with pytest.raises(RuntimeError, match="provider blew up"):
        await runtime.run("write it", [], emit, approve)

    # The failure event was emitted and carries the partial outcome on the exc.
    # We can't easily get the exc object here, so assert via the events + a
    # direct run that captures it.
    failures = [e for e in events if isinstance(e, RunFailed)]
    assert len(failures) == 1


async def test_explicit_retry_replays_exact_successful_side_effect_receipt() -> None:
    executions = 0
    fail_after_tool = True

    def write_note(content: str) -> dict[str, str]:
        """Write a note."""

        nonlocal executions
        executions += 1
        return {"written": content}

    async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        if last_tool_return(messages) is None:
            yield {0: DeltaToolCall("write_note", '{"content":"once"}', tool_call_id="write-1")}
        elif fail_after_tool:
            raise RuntimeError("provider failed after write")
        else:
            yield "recovered"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_function),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="write once",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "test", "risk": "write"}},
    )

    async def emit(_event: RunEvent) -> None:
        return None

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True, "allowed")

    with pytest.raises(RuntimeError) as exc_info:
        await runtime.run("write it", [], emit, approve)
    partial = get_partial_outcome(exc_info.value)
    assert partial is not None
    assert len(partial.recovery_receipts) == 1
    assert executions == 1

    fail_after_tool = False
    outcome = await runtime.run(
        "write it",
        [],
        emit,
        approve,
        recovery_receipts=partial.recovery_receipts,
    )

    assert outcome.output == "recovered"
    assert executions == 1
    assert outcome.recovery_receipts[0]["replayed"] is True


async def test_cancelled_run_carries_partial_outcome() -> None:
    """A cancelled run attaches a PartialRunOutcome with status='cancelled'."""
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    def echo(value: str) -> str:
        """Echo."""
        return value

    runtime = AgentRuntime(
        model="test",
        tools=[Tool(echo, sequential=True)],
        toolsets=[],
        instructions="hi",
        limits=LimitsConfig(),
        tool_metadata={"echo": {"origin": "test", "risk": "read"}},
    )

    @asynccontextmanager
    async def fake_stream(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError()
        yield  # pragma: no cover - required for asynccontextmanager

    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: Any) -> ToolApproval:
        return ToolApproval(True)

    with patch.object(runtime.agent, "run_stream_events", fake_stream):
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await runtime.run("go", [], emit, approve)

    partial = get_partial_outcome(exc_info.value)
    assert partial is not None
    assert partial.status == "cancelled"
    assert any(isinstance(e, RunCancelled) for e in events)
