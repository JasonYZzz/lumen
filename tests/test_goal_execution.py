from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from test_automation import resources_for

from lumen.api import create_web_app
from lumen.application import (
    ContinueAssistantDiscussion,
    ControlGoal,
    CreateSession,
    EditAssistant,
    GetGoalDetail,
    InvalidStateError,
    SetCollaborationMode,
    StartRun,
    WorkspaceHost,
)
from lumen.automation.models import (
    AssistantEdit,
    AutomationCommand,
    AutomationInput,
    ContextRef,
    Definition,
    Goal,
    GoalCommand,
    GoalExecutionStatus,
    GoalInput,
    Milestone,
    Occurrence,
    Phase,
    Result,
)
from lumen.automation.store import AssistantConflict, AutomationStore
from lumen.config import LimitsConfig
from lumen.runtime import AgentRuntime


async def wait_goal(host: WorkspaceHost, status: GoalExecutionStatus) -> Goal:
    async def wait() -> Goal:
        while True:
            goal = host.automations.store.get("goal", "goal", Goal)
            if goal.execution and goal.execution.status is status:
                return goal
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(wait(), 5)


async def create_goal(host: WorkspaceHost, *, session_id: str | None = None) -> None:
    await host.dispatch(EditAssistant(AssistantEdit(action="goal", id="goal",
        goal=GoalInput(title="Verify outcome"), source_session_id=session_id)))


async def start_goal(host: WorkspaceHost, turn_limit: int | None = None) -> None:
    goal = host.automations.store.get("goal", "goal", Goal)
    await host.dispatch(ControlGoal(GoalCommand(action="start", goal_id="goal", request_id="start",
        expected_revision=goal.revision, criteria="Verify the outcome using actual evidence",
        turn_limit=turn_limit)))


@pytest.mark.parametrize("turn_limit", [None, 1])
async def test_goal_continues_after_persistence_and_stops_for_review_or_budget(
    tmp_path: Path, turn_limit: int | None,
) -> None:
    step = 0
    verifications = 0

    def verify_outcome() -> str:
        nonlocal verifications
        verifications += 1
        return "Verified actual outcome"

    async def stream(_messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal step
        assert "report_goal_progress" in {tool.name for tool in info.function_tools}
        step += 1
        turn = (step - 1) // 3 + 1
        if step % 3 == 1:
            yield {0: DeltaToolCall("verify_outcome", "{}", tool_call_id=f"verify-{turn}")}
        elif step % 3 == 2:
            yield {0: DeltaToolCall("report_goal_progress", json.dumps({"report": {
                "status": "continue" if turn == 1 else "complete", "summary": f"Verified turn {turn}",
                "evidence_call_ids": [f"verify-{turn}"],
            }}), tool_call_id=f"report-{turn}")}
        else:
            yield f"Reviewable output {turn}"

    resources = resources_for(tmp_path, strict_background=False)
    resources.runtime = AgentRuntime(model=FunctionModel(stream_function=stream),
        tools=[Tool(verify_outcome)], toolsets=[], instructions="Verify", limits=LimitsConfig(),
        tool_metadata={"verify_outcome": {"origin": "test", "risk": "read"}})
    host = await WorkspaceHost(resources).open()  # type: ignore[arg-type]
    try:
        await create_goal(host)
        await start_goal(host, turn_limit)
        expected = GoalExecutionStatus.BUDGET_REACHED if turn_limit else GoalExecutionStatus.REVIEW
        goal = await wait_goal(host, expected)
        assert goal.execution
        assert goal.status == "active"  # Agent submits evidence; user accepts the durable goal.
        assert goal.execution.turns == verifications == (1 if turn_limit else 2)
        assert goal.primary_session_id
        turns = resources.session_repository.load(goal.primary_session_id).turns
        assert len(turns) == goal.execution.turns
        assert all(turn.status == "completed" for turn in turns)
        assert goal.execution.evidence[-1].call_ids == (f"verify-{verifications}",)
        # Retry of the same control does not start another run or renew the budget.
        await host.dispatch(ControlGoal(GoalCommand(action="start", goal_id="goal", request_id="start",
            expected_revision=1, criteria="Verify the outcome using actual evidence", turn_limit=turn_limit)))
        assert verifications == len(turns)
        if not turn_limit:
            await host.dispatch(EditAssistant(AssistantEdit(action="goal", id=goal.id,
                expected_revision=goal.revision,
                goal=GoalInput(title=goal.title, status="completed"))))
            accepted = host.automations.store.get("goal", goal.id, Goal)
            assert accepted.execution and accepted.execution.status is GoalExecutionStatus.ACCEPTED
    finally:
        await host.close()


@pytest.mark.parametrize("report_status,call_ids", [
    ("continue", []), ("complete", []), ("complete", ["forged-call"]), ("blocked", []),
])
async def test_goal_without_actual_evidence_never_loops_or_submits_review(
    tmp_path: Path, report_status: str, call_ids: list[str],
) -> None:
    step = 0

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal step
        step += 1
        if step == 1:
            yield {0: DeltaToolCall("report_goal_progress", json.dumps({"report": {
                "status": report_status, "summary": "Need evidence or user input",
                "evidence_call_ids": call_ids,
            }}), tool_call_id="report")}
        else:
            yield "No actual execution evidence"

    resources = resources_for(tmp_path, strict_background=False)
    resources.runtime = AgentRuntime(model=FunctionModel(stream_function=stream), tools=[], toolsets=[],
        instructions="Test", limits=LimitsConfig(), tool_metadata={})
    host = await WorkspaceHost(resources).open()  # type: ignore[arg-type]
    try:
        await create_goal(host)
        await start_goal(host)
        goal = await wait_goal(host, GoalExecutionStatus.BLOCKED)
        assert goal.execution and goal.execution.turns == 1
        assert step == 2
    finally:
        await host.close()


async def test_source_conversation_is_retained_and_unowned_source_is_rejected(tmp_path: Path) -> None:
    resources = resources_for(tmp_path, strict_background=False)
    host = await WorkspaceHost(resources).open()  # type: ignore[arg-type]
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "Existing constraints", "original"))
        _ = [event async for event in host.subscribe(started.run_id)]
        original = resources.session_repository.load(session.session_id).turns
        await create_goal(host, session_id=session.session_id)
        discussion = await host.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        assert discussion.data["session_id"] == session.session_id
        assert resources.session_repository.load(session.session_id).turns == original
        await host.dispatch(EditAssistant(AssistantEdit(action="goal", id="second",
            goal=GoalInput(title="Related outcome"), source_session_id=session.session_id)))
        assert {ref.id for ref in host.automations.store.discussion_context(session.session_id)} == {
            "goal", "second",
        }
        await host.dispatch(EditAssistant(AssistantEdit(action="trash_goal", id="goal", expected_revision=1)))
        assert [ref.id for ref in host.automations.store.discussion_context(session.session_id)] == ["second"]
        continued = await host.dispatch(StartRun(
            session.session_id, "Continue the original discussion", "next",
        ))
        assert [event async for event in host.subscribe(continued.run_id)][-1].type == "run.completed"
        with pytest.raises(InvalidStateError):
            await host.dispatch(EditAssistant(AssistantEdit(action="goal", id="bad",
                goal=GoalInput(title="Bad"), source_session_id="../unknown")))
        foreign = resources.session_repository.create(agent_name="foreign", model_id="test")
        with pytest.raises(InvalidStateError, match="当前工作区"):
            await host.dispatch(EditAssistant(AssistantEdit(action="goal", id="foreign",
                goal=GoalInput(title="Foreign"), source_session_id=foreign.id)))
    finally:
        await host.close()


@pytest.mark.parametrize("target", ["completed", "archived"])
def test_closing_goal_requires_follow_up_choice_and_pause_cancels_only_queued_work(
    tmp_path: Path, target: Literal["completed", "archived"],
) -> None:
    store = AutomationStore(tmp_path / "state.db")
    store.open()
    try:
        now = datetime.now(UTC)
        identity = store.command(AutomationCommand(action="create", request_id="create",
            spec=AutomationInput(name="Review", intent="Read", paths=("README.md",))), "fp", now)
        trial_id = store.command(AutomationCommand(action="trial", request_id="trial", id=identity,
                                                   expected_revision=1), "fp", now)
        trial = store.get("occurrence", trial_id, Occurrence)
        store.settle(trial.model_copy(update={"result": Result.COMPLETE, "fingerprint": "fp"}))
        store.command(AutomationCommand(action="enable", request_id="enable", id=identity,
                                        expected_revision=1), "fp", now)
        definition = store.get("definition", identity, Definition)
        assert definition.next_due
        store.materialize(definition.next_due)
        goal = GoalInput(title="Review", automation_ids=(identity,),
                         milestones=(Milestone(id="next", title="Confirm"),))
        store.edit(AssistantEdit(action="goal", id="goal", goal=goal))
        closing = AssistantEdit(action="goal", id="goal", expected_revision=1,
                                 goal=goal.model_copy(update={"status": target}))
        with pytest.raises(AssistantConflict, match="持续跟进"):
            store.edit(closing)
        assert store.get("goal", "goal", Goal).revision == 1
        store.edit(closing.model_copy(update={"follow_up_policy": "keep"}))
        assert store.get("definition", identity, Definition).enabled
        store.edit(AssistantEdit(action="goal", id="goal", expected_revision=2, goal=goal))
        store.edit(closing.model_copy(update={"expected_revision": 3, "follow_up_policy": "pause"}))
        assert not store.get("definition", identity, Definition).enabled
        assert not store.pending()
        assert store.get("goal", "goal", Goal).status == target
        assert store.get("occurrence", trial_id, Occurrence).phase is Phase.SETTLED
    finally:
        store.close()


async def test_pause_and_restart_preserve_progress_without_replaying(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        yield "Finished"

    resources = resources_for(tmp_path, strict_background=False)
    resources.runtime = AgentRuntime(model=FunctionModel(stream_function=stream), tools=[], toolsets=[],
        instructions="Wait", limits=LimitsConfig(), tool_metadata={})
    host = await WorkspaceHost(resources).open()  # type: ignore[arg-type]
    try:
        await create_goal(host)
        await start_goal(host, 1)
        await asyncio.wait_for(entered.wait(), 3)
        goal = host.automations.store.get("goal", "goal", Goal)
        await host.dispatch(ControlGoal(GoalCommand(action="pause", goal_id="goal", request_id="pause",
                                                   expected_revision=goal.revision)))
        paused = host.automations.store.get("goal", "goal", Goal)
        assert paused.execution and paused.execution.status is GoalExecutionStatus.PAUSED
        assert paused.execution.turns == 1
        # Simulate a crash after durable activation, before admission; recovery pauses it.
        host.automations.store.control_goal(GoalCommand(action="resume", goal_id="goal", request_id="resume",
            expected_revision=paused.revision, criteria=paused.execution.criteria, turn_limit=2))
    finally:
        await host.close()
    restarted = await WorkspaceHost(resources_for(tmp_path, strict_background=False)).open()  # type: ignore[arg-type]
    try:
        goal = restarted.automations.store.get("goal", "goal", Goal)
        assert goal.execution and goal.execution.status is GoalExecutionStatus.PAUSED
        assert goal.execution.turns == 1
    finally:
        await restarted.close()


async def test_goal_control_api_keeps_auth_origin_and_plan_boundaries(tmp_path: Path) -> None:
    host = await WorkspaceHost(resources_for(tmp_path, strict_background=False)).open()  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="test-goals", api_only=True)
    try:
        await create_goal(host)
        discussion = await host.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        await host.dispatch(SetCollaborationMode(str(discussion.data["session_id"]), "plan"))
        body = {"action": "start", "goal_id": "goal", "request_id": "api", "expected_revision": 1,
                "criteria": "Verify", "turn_limit": 1}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver",
        ) as client:
            assert (await client.post("/api/v1/assistant/goals/control", json=body)).status_code == 401
            await client.get("/auth/exchange?token=test-goals")
            client.headers["Origin"] = "http://testserver"
            forbidden = await client.post("/api/v1/assistant/goals/control", json=body,
                                           headers={"Origin": "http://evil.example"})
            assert forbidden.status_code == 403
            response = await client.post("/api/v1/assistant/goals/control", json=body)
            assert response.status_code == 400
            assert "规划模式" in response.text
            detail = (await host.dispatch(GetGoalDetail("goal"))).data
            assert detail["goal"]["execution"] is None
    finally:
        await host.close()
