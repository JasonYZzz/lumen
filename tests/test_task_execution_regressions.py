from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.agent_loop import LoopCompletionRejected, LoopProviderFailure
from lumen.completion import CompletionPolicy
from lumen.config import LimitsConfig, ModelSettingsConfig
from lumen.events import PlanCreated, PlanUpdated, RunEvent, RunFailed, ToolCallFinished
from lumen.plan import EvidenceKind, EvidenceReceipt, PlanLifecycle, PlanState, PlanStep, StepStatus
from lumen.runtime import AgentRuntime, get_partial_outcome


@pytest.mark.parametrize("name", ["qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash"])
def test_qwen_preserves_provider_default_without_explicit_thinking(name: str) -> None:
    supplied = {"max_tokens": 131_072}
    config = ModelSettingsConfig(id=f"anthropic:{name}", settings=supplied)
    assert config.settings == {"max_tokens": 131_072}
    assert supplied == {"max_tokens": 131_072}


@pytest.mark.parametrize("settings", [
    {"thinking": False}, {"thinking": "low"}, {"thinking": True}, {"thinking": None},
    {"anthropic_thinking": {"type": "enabled", "budget_tokens": 2048}},
    {"anthropic_effort": "low"}, {"extra_body": {"thinking": {"type": "enabled"}}},
])
def test_qwen_explicit_thinking_choice_is_preserved(settings: dict[str, Any]) -> None:
    assert ModelSettingsConfig(id="anthropic:qwen3.8-max", settings=settings).settings == settings


@pytest.mark.parametrize("model_id", [
    "anthropic:claude-sonnet-4-6", "anthropic:qwen3.8-max-preview",
    "anthropic:qwen3.8-2.4t-a95b", "openai:qwen3.8-max", "test",
    "openai:deepseek-v4-flash", "openai:deepseek-v4-pro",
])
def test_unknown_or_other_protocol_models_keep_upstream_defaults(model_id: str) -> None:
    assert ModelSettingsConfig(id=model_id).settings == {}


@pytest.mark.parametrize("batched", [False, True])
async def test_plan_evidence_is_usable_by_model_without_replanning(batched: bool) -> None:
    phase = 0

    def inspect_report() -> str:
        """Inspect a report's required sections."""
        return "All required sections present"

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal phase
        phase += 1
        stage = (1 if phase == 1 else 4 if phase == 2 else 8) if batched else phase
        if stage == 1:
            name, args = "set_plan", {"goal": "Deliver report", "steps": [
                {"id": "verify", "title": "Verify report", "acceptance_criteria": [
                    {"id": "sections", "description": "Required sections present"},
                ]},
                {"id": "deliver", "title": "Deliver report", "depends_on": ["verify"]},
            ]}
        elif stage == 2:
            name, args = "update_step", {"step_id": "verify", "status": "in_progress"}
        elif stage == 3:
            name, args = "inspect_report", {}
        elif stage == 4:
            request = messages[-1]
            assert isinstance(request, ModelRequest)
            result = request.parts[-1]
            assert isinstance(result, ToolReturnPart)
            content = cast(dict[str, Any], result.content)
            assert content["result"] == "All required sections present"
            receipt = cast(dict[str, Any], content["plan_evidence"])
            assert receipt["passed"] is True
            name, args = "link_evidence", {
                "step_id": "verify", "evidence_id": receipt["id"], "criterion_ids": ["sections"],
            }
        elif stage == 5:
            name, args = "update_step", {"step_id": "verify", "status": "completed"}
        elif stage == 6:
            name, args = "update_step", {"step_id": "deliver", "status": "in_progress"}
        elif stage == 7:
            name, args = "update_step", {"step_id": "deliver", "status": "completed"}
        else:
            yield "Report delivered."
            return
        calls: list[tuple[str, dict[str, Any]]] = [(name, args)]
        if batched and phase == 1:
            calls.extend([
                ("update_step", {"step_id": "verify", "status": "in_progress"}),
                ("inspect_report", {}),
            ])
        elif batched and phase == 2:
            calls.extend([
                ("update_step", {"step_id": "verify", "status": "completed"}),
                ("update_step", {"step_id": "deliver", "status": "in_progress"}),
                ("update_step", {"step_id": "deliver", "status": "completed"}),
            ])
        yield {i: DeltaToolCall(tool, json.dumps(arguments), tool_call_id=f"call-{phase}-{i}")
               for i, (tool, arguments) in enumerate(calls)}

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[Tool(inspect_report)], toolsets=[],
        instructions="Deliver and verify.", limits=LimitsConfig(request_count=8),
        tool_metadata={"inspect_report": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    outcome = await runtime.run("Deliver report", [], emit, lambda _: pytest.fail("no approval"))
    assert outcome.status == "completed"
    assert outcome.plan.lifecycle is PlanLifecycle.COMPLETED
    assert outcome.plan.revision == 1
    assert outcome.usage["tool_calls"] == 7
    assert outcome.usage["requests"] == (3 if batched else 8)
    observations = [d for d in outcome.diagnostics if d.get("kind") == "request_observed"]
    assert sum(d["control_only"] for d in observations) == (1 if batched else 6)
    assert sum(isinstance(event, PlanCreated) for event in events) == 1
    assert any(isinstance(event, PlanUpdated) and [s.status.value for s in event.plan.steps]
               == ["completed", "pending"] for event in events)
    assert not any(isinstance(event, ToolCallFinished) and event.is_error for event in events)
    # UI/audit output remains the original result; evidence is a provider projection.
    finished = next(e for e in events if isinstance(e, ToolCallFinished) and e.name == "inspect_report")
    assert finished.result == "All required sections present"


@pytest.mark.parametrize("verify_after_write", [True, False])
async def test_same_batch_verification_uses_completion_order_after_restored_evidence(
    verify_after_write: bool,
) -> None:
    phase = 0
    executed: list[str] = []

    def write_file() -> str:
        """Simulate the mutation executor's receipt."""
        executed.append("write_file")
        return "written"

    def run_command() -> dict[str, int]:
        """Simulate a passing verification command."""
        executed.append("run_command")
        return {"exit_code": 0}

    order = ["write_file", "run_command"] if verify_after_write else ["run_command", "write_file"]

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal phase
        phase += 1
        if phase == 1:
            yield {i: DeltaToolCall(name, "{}", tool_call_id=name) for i, name in enumerate(order)}
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[Tool(write_file), Tool(run_command)], toolsets=[],
        instructions="Test receipts.", limits=LimitsConfig(),
        tool_metadata={name: {"risk": "read", "origin": "test"} for name in order},
    )
    plan = PlanState(revision=1, approved_revision=1,
        steps=[PlanStep(id="done", title="Existing completed work", status=StepStatus.COMPLETED)],
        evidence=[EvidenceReceipt(id="prior", kind=EvidenceKind.COMMAND, source_id="prior-command",
                                  summary="Prior task verification", passed=True, sequence=500)])

    async def emit(_event: RunEvent) -> None:
        pass

    async def run():  # type: ignore[no-untyped-def]
        return await runtime.run("Finish work", [], emit, lambda _: pytest.fail("no approval"),
                                 plan=plan, completion_policy=CompletionPolicy(max_retries=0))

    if verify_after_write:
        outcome = await run()
        assert outcome.status == "completed"
        assert [r.source_id for r in outcome.plan.evidence] == ["prior-command", *order]
        assert 500 < outcome.plan.evidence[1].sequence < outcome.plan.evidence[2].sequence
    else:
        with pytest.raises(LoopCompletionRejected, match="no passing verification"):
            await run()
    assert executed == order
    assert phase == 2


@pytest.mark.parametrize(("declared_safe", "sequential"), [(False, False), (True, False), (True, True)])
async def test_runtime_adapter_requires_explicit_safe_concurrency(
    declared_safe: bool, sequential: bool,
) -> None:
    phase = 0
    active = 0
    peak = 0
    pair_started = asyncio.Event()
    parallel = declared_safe and not sequential

    async def inspect() -> str:
        """Read the fixture."""
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if active == 2:
                pair_started.set()
            if parallel:
                await asyncio.wait_for(pair_started.wait(), 0.5)
            else:
                await asyncio.sleep(0)
            return "checked"
        finally:
            active -= 1

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal phase
        phase += 1
        if phase == 1:
            yield {i: DeltaToolCall("inspect", "{}", tool_call_id=f"inspect-{i}") for i in range(2)}
        else:
            yield "done"

    metadata = {"origin": "test", "risk": "read"}
    if declared_safe:
        metadata["concurrency"] = "parallel_safe"
    runtime = AgentRuntime(model=FunctionModel(stream_function=stream),
        tools=[Tool(inspect, sequential=sequential)], toolsets=[], instructions="Inspect.",
        limits=LimitsConfig(), tool_metadata={"inspect": metadata})
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    outcome = await runtime.run("Inspect", [], emit, lambda _: pytest.fail("no approval"))
    assert outcome.status == "completed"
    assert peak == (2 if parallel else 1)
    assert not any(isinstance(e, ToolCallFinished) and e.is_error for e in events)


async def test_request_diagnostics_capture_configured_thinking_without_unrelated_settings() -> None:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "done"

    runtime = AgentRuntime(model=FunctionModel(stream_function=stream), tools=[], toolsets=[],
        instructions="Inspect.", limits=LimitsConfig(), tool_metadata={},
        model_settings={"thinking": "low", "max_tokens": 2048,
                        "extra_body": {"private_marker": "must-not-enter-diagnostics"}})

    async def emit(_event: RunEvent) -> None:
        pass

    outcome = await runtime.run("Inspect", [], emit, lambda _: pytest.fail("no approval"))
    prepared = next(d for d in outcome.diagnostics if d.get("kind") == "model_request_prepared")
    assert prepared["configured_thinking"] == {"thinking": "low"}
    assert prepared["max_tokens"] == 2048
    assert "must-not-enter-diagnostics" not in json.dumps(outcome.diagnostics)


async def test_stream_failure_has_details_and_retains_tool_usage_without_creating_plan() -> None:
    phase = 0

    def inspect_report() -> str:
        """Read the report."""
        return "report contents"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal phase
        phase += 1
        if phase == 1:
            yield {0: DeltaToolCall("inspect_report", "{}", tool_call_id="read")}
        else:
            yield "Preparing report"
            raise AssertionError()

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[Tool(inspect_report)], toolsets=[],
        instructions="Inspect.", limits=LimitsConfig(),
        tool_metadata={"inspect_report": {"origin": "test", "risk": "read"}},
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    with pytest.raises(LoopProviderFailure, match="AssertionError") as error:
        await runtime.run("Inspect report", [], emit, lambda _: pytest.fail("no approval"))
    partial = get_partial_outcome(error.value)
    assert partial is not None
    assert partial.status == "failed"
    assert partial.usage["tool_calls"] == 1
    assert any(isinstance(event, RunFailed) and event.message for event in events)
    assert not any(isinstance(event, PlanCreated | PlanUpdated) for event in events)
