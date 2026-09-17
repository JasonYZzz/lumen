from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import LimitsConfig, load_config
from lumen.events import (
    CommentaryDelta,
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunEvent,
    TextDelta,
    TextRetracted,
    ToolApprovalResolved,
    ToolCallFinished,
)
from lumen.plan import StepStatus
from lumen.resources import ResourceManager
from lumen.runtime import AgentRuntime, ToolApproval


def last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


def projected_assistant_text(events: Sequence[RunEvent]) -> str:
    text = ""
    for event in events:
        if isinstance(event, TextDelta):
            text += event.text
        elif isinstance(event, TextRetracted):
            text = text[: -event.characters] if event.characters else text
    return text


def _delta(name: str, args: dict[str, Any], call_id: str) -> dict[int, DeltaToolCall]:
    return {0: DeltaToolCall(name, json.dumps(args), tool_call_id=call_id)}


def _workspace_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  name: integration
  model:
    id: test
  limits: {}
tools:
  builtins: [read_file, write_file, edit_file, run_command]
sessions:
  directory: sessions
context:
  enabled: false
sandbox:
  mode: disabled
""",
        encoding="utf-8",
    )
    return config_path


async def test_end_to_end_scenario_with_approvals(tmp_path: Path) -> None:
    """Drive the full plan + progress + read + write + run + update cycle."""

    config = load_config(_workspace_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    (tmp_path / "config.yaml").write_text("hello: world\n", encoding="utf-8")

    async with manager:
        state: dict[str, Any] = {"phase": "plan"}

        async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
            result = last_tool_return(messages)
            if state["phase"] == "plan":
                state["phase"] = "progress"
                yield _delta(
                    "set_plan",
                    {"steps": [{"id": "fix", "title": "Patch config"}]},
                    "plan-1",
                )
            elif state["phase"] == "progress":
                state["phase"] = "read"
                yield _delta(
                    "report_progress",
                    {"summary": "Reading the config.", "next_action": "Edit"},
                    "progress-1",
                )
            elif state["phase"] == "read":
                state["phase"] = "edit"
                yield _delta("read_file", {"path": "config.yaml"}, "read-1")
            elif state["phase"] == "edit":
                state["phase"] = "run"
                yield _delta(
                    "edit_file",
                    {"path": "config.yaml", "find": "hello", "replace": "greeting"},
                    "edit-1",
                )
            elif state["phase"] == "run":
                state["phase"] = "update"
                yield _delta("run_command", {"argv": [sys.executable, "-c", "pass"]}, "run-1")
            elif state["phase"] == "update":
                state["phase"] = "done"
                yield _delta(
                    "update_step",
                    {"step_id": "fix", "status": "completed"},
                    "update-1",
                )
            else:
                _ = result
                yield "All set; config patched."

        # Build a runtime identical to the manager's, but with our deterministic
        # FunctionModel in place of TestModel. We reuse the registered local
        # tools and tool metadata so capability/approval paths still work.
        runtime = AgentRuntime(
            model=FunctionModel(stream_function=model_function),
            tools=manager.local_tools,
            toolsets=[],
            instructions=manager.instructions,
            limits=config.agent.limits,
            tool_metadata=manager.tool_metadata,
        )

        events: list[RunEvent] = []

        async def emit(event: RunEvent) -> None:
            events.append(event)

        async def approve(request: Any) -> ToolApproval:
            assert request.name in {"write_file", "edit_file", "run_command"}
            return ToolApproval(approved=True, message="user approved")

        outcome = await runtime.run("patch the config", [], emit, approve)

        # Persist the outcome through the manager's repository so the load
        # assertion below reflects what a real session looks like.
        session = manager.session_repository.create(
            agent_name=config.agent.name, model_id=manager.active_model_config().id
        )
        manager.session_repository.append_turn(
            session.id,
            user_input="patch the config",
            messages=outcome.new_messages,
            approvals=outcome.approvals,
            usage=outcome.usage,
            status="completed",
            plan=outcome.plan,
            diagnostics=outcome.diagnostics,
            compaction=outcome.compaction,
        )

    # File was edited exactly once.
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "greeting: world\n"
    # Run command produced zero exit.
    run_finished = next(
        event for event in events if isinstance(event, ToolCallFinished) and event.name == "run_command"
    )
    assert "'exit_code': 0" in run_finished.result or '"exit_code": 0' in run_finished.result
    assert run_finished.exit_code == 0
    run_diagnostic = next(item for item in outcome.diagnostics if item.get("name") == "run_command")
    assert run_diagnostic["status"] == "success"
    assert run_diagnostic["exit_code"] == 0
    # Event order is semantic: plan created, progress reported, then plan updated.
    semantic_order = [
        type(event) for event in events if isinstance(event, (PlanCreated, ProgressReported, PlanUpdated))
    ]
    assert semantic_order[:2] == [PlanCreated, ProgressReported]
    assert semantic_order[-1] is PlanUpdated
    # Executor-created receipts also update plan state; the model cannot forge them.
    assert len(outcome.plan.evidence) == 3
    # Final output never contains commentary text.
    final = projected_assistant_text(events)
    assert "All set; config patched." in final
    assert "Reading the config." not in final  # that was progress, not final
    # Approvals were resolved approved=True for each capability call.
    approvals = [event for event in events if isinstance(event, ToolApprovalResolved)]
    assert len(approvals) == 2  # edit_file + run_command
    assert all(event.approved for event in approvals)
    # Plan snapshot in the outcome reflects the completed step.
    assert outcome.plan.steps[0].status is StepStatus.COMPLETED

    # Reload the session: plan and active history are restored.
    repo = manager.session_repository
    sessions = repo.list()
    assert sessions, "session should have been created"
    loaded = repo.load(sessions[0].id)
    assert loaded.plan.steps[0].status is StepStatus.COMPLETED
    assert loaded.history, "active history should contain the new turn"


async def test_denial_recovery_scenario(tmp_path: Path) -> None:
    """A denied run_command drives the model to a public fallback + read."""

    config = load_config(_workspace_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    (tmp_path / "fallback.txt").write_text("fallback content", encoding="utf-8")

    async with manager:
        state: dict[str, Any] = {"phase": "plan"}

        async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
            if state["phase"] == "plan":
                state["phase"] = "try-run"
                yield _delta(
                    "set_plan",
                    {"steps": [{"id": "go", "title": "Try and recover"}]},
                    "p1",
                )
            elif state["phase"] == "try-run":
                state["phase"] = "recover"
                yield _delta("run_command", {"argv": ["false"]}, "r1")
            elif state["phase"] == "recover":
                state["phase"] = "report"
                yield _delta(
                    "report_progress",
                    {"summary": "Falling back to a static file.", "next_action": None},
                    "p2",
                )
            elif state["phase"] == "report":
                state["phase"] = "read"
                yield _delta("read_file", {"path": "fallback.txt"}, "r2")
            elif state["phase"] == "read":
                state["phase"] = "done"
                yield _delta("update_step", {"step_id": "go", "status": "completed"}, "finish-go")
            else:
                yield "Recovered via fallback file."

        runtime = AgentRuntime(
            model=FunctionModel(stream_function=model_function),
            tools=manager.local_tools,
            toolsets=[],
            instructions=manager.instructions,
            limits=config.agent.limits,
            tool_metadata=manager.tool_metadata,
        )

        events: list[RunEvent] = []

        async def emit(event: RunEvent) -> None:
            events.append(event)

        async def deny(request: Any) -> ToolApproval:
            assert request.name == "run_command"
            return ToolApproval(approved=False, message="user denied")

        outcome = await runtime.run("try and recover", [], emit, deny)

    # The run_command was denied and the process never ran. Pydantic AI still
    # emits a FunctionToolResultEvent for the denied call, but it carries an
    # error result with no exit code rather than a real command outcome.
    denial = next(event for event in events if isinstance(event, ToolApprovalResolved))
    assert denial.approved is False
    run_finished_calls = [
        event for event in events if isinstance(event, ToolCallFinished) and event.name == "run_command"
    ]
    assert len(run_finished_calls) == 1
    denied_call = run_finished_calls[0]
    assert denied_call.is_error is True
    assert denied_call.exit_code is None
    denied_diagnostic = next(
        item for item in outcome.diagnostics
        if item.get("name") == "run_command" and item.get("status") == "denied"
    )
    assert denied_diagnostic["error_category"] == "denied"
    # The result text mentions the denial, never a real stdout/stderr payload.
    assert "denied" in denied_call.result.lower()
    # The model reported a public fallback and used read_file.
    assert any(isinstance(event, ProgressReported) and "fall" in event.summary.lower() for event in events)
    assert any(isinstance(event, ToolCallFinished) and event.name == "read_file" for event in events)
    final = projected_assistant_text(events)
    assert "Recovered via fallback file." in final
    assert outcome.plan.steps[0].status in {
        StepStatus.PENDING,
        StepStatus.IN_PROGRESS,
        StepStatus.COMPLETED,
    }


async def test_commentary_is_separated_from_final_answer(tmp_path: Path) -> None:
    config = load_config(_workspace_config(tmp_path))
    manager = ResourceManager(config, workspace=tmp_path)
    (tmp_path / "x").write_text("data", encoding="utf-8")

    async with manager:

        async def model_function(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
            result = last_tool_return(messages)
            if result is None:
                yield "Inspecting."  # commentary
                yield _delta("read_file", {"path": "x"}, "rc1")
            else:
                _ = result
                yield "Done."  # final

        runtime = AgentRuntime(
            model=FunctionModel(stream_function=model_function),
            tools=manager.local_tools,
            toolsets=[],
            instructions=manager.instructions,
            limits=config.agent.limits,
            tool_metadata=manager.tool_metadata,
        )

        events: list[RunEvent] = []

        async def emit(event: RunEvent) -> None:
            events.append(event)

        async def approve(_request: Any) -> ToolApproval:
            return ToolApproval(approved=True, message="ok")

        outcome = await runtime.run("go", [], emit, approve)

    commentary = "".join(event.text for event in events if isinstance(event, CommentaryDelta))
    final = projected_assistant_text(events)
    assert commentary == "Inspecting."
    assert final == "Done."
    assert outcome.output == "Done."


# Keep imports live for typing parity / future tests.
_ = LimitsConfig
