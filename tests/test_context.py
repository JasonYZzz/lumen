from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.config import ContextConfig
from lumen.context import (
    CompactionRecord,
    ContextBudgetExceeded,
    ContextManager,
    ContextReservation,
    ContextSummary,
    RequestBudgetEstimator,
    estimate_message_tokens,
    retain_recent_tokens,
    retain_recent_turns,
    validate_active_history,
)
from lumen.events import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    RunEvent,
)
from lumen.plan import PlanState


def _summary_function_model(content: str, *, fail: bool = False) -> FunctionModel:
    """Return a FunctionModel that yields ``content`` as the assistant text.

    ``ContextManager`` uses the non-streamed path so we expose ``function``
    (not ``stream_function``) which returns a ``ModelResponse`` directly.
    """

    def function(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if fail:
            raise RuntimeError("summary model exploded")
        return ModelResponse(parts=[TextPart(content=content)])

    return FunctionModel(function=function)


def test_estimate_tokens_is_deterministic() -> None:
    messages = [ModelRequest(parts=[UserPromptPart(content="x" * 400)])]
    # Serialised UTF-8 length / 4 rounded up: 400 / 4 = 100.
    assert estimate_message_tokens(messages) == 100


def test_estimate_tokens_handles_empty_messages() -> None:
    assert estimate_message_tokens([]) == 0


def test_recent_turns_keeps_complete_user_boundaries() -> None:
    history: list[ModelMessage] = []
    for number in range(3):
        history.extend(
            [
                ModelRequest(parts=[UserPromptPart(content=f"question {number}")]),
                ModelResponse(parts=[TextPart(content=f"answer {number}")]),
            ]
        )
    kept = retain_recent_turns(history, 2)
    user_count = sum(
        isinstance(part, UserPromptPart)
        for message in kept
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
    assert user_count == 2
    assert kept[-1] == history[-1]


def test_retain_recent_turns_returns_full_history_when_short() -> None:
    history = [ModelRequest(parts=[UserPromptPart(content="q1")])]
    assert retain_recent_turns(history, 4) == history


def test_recent_token_window_never_starts_with_assistant_response() -> None:
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="question")]),
        ModelResponse(parts=[TextPart(content="answer")]),
    ]

    kept = retain_recent_tokens(history, keep_tokens=1)

    assert kept == history
    assert validate_active_history(kept) == []


def test_validate_active_history_rejects_assistant_response_as_first_message() -> None:
    history = [ModelResponse(parts=[TextPart(content="orphaned answer")])]

    assert validate_active_history(history) == ["active history does not start with a user request"]


def test_context_summary_round_trips() -> None:
    summary = ContextSummary(
        goals=["finish the task"],
        constraints=["do not write outside cwd"],
        completed=["step one"],
        current_plan=["step two: pending"],
        important_files=["src/main.py"],
        key_facts=["the answer is 42"],
        failures_and_approvals=["write_file denied by user"],
        outstanding=["verify build"],
    )
    # All lists are exposed verbatim.
    assert summary.goals == ["finish the task"]
    assert summary.outstanding == ["verify build"]


def _summary_stream_function(content: str, *, fail: bool = False):  # type: ignore[no-untyped-def]
    """Backwards-compat alias retained for callers that build stream functions."""

    async def stream(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        if fail:
            raise RuntimeError("summary model exploded")
        yield content

    return stream


async def test_context_manager_compacts_when_over_limit() -> None:
    # Two big user prompts so the estimate clearly exceeds the soft limit.
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="a" * 4000)]),
        ModelResponse(parts=[TextPart(content="b" * 4000)]),
        ModelRequest(parts=[UserPromptPart(content="c" * 100)]),
    ]
    config = ContextConfig(
        enabled=True, soft_token_limit=100, keep_recent_tokens=1000, summary_max_tokens=2000
    )
    summary_yaml = (
        '{"goals":["finish"],"constraints":[],"completed":[],"current_plan":[],'
        '"important_files":[],"key_facts":[],"failures_and_approvals":[],"outstanding":[]}'
    )
    manager = ContextManager(config, model=_summary_function_model(summary_yaml))
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    prepared = await manager.prepare(history, PlanState(), [], emit)

    assert prepared.compaction is not None
    assert isinstance(prepared.compaction.summary, ContextSummary)
    assert prepared.compaction.source_message_count == len(history)
    # The prepared history starts with one summary SystemPromptPart.
    assert isinstance(prepared.history[0], ModelRequest)
    assert isinstance(prepared.history[0].parts[0], SystemPromptPart)
    assert any(isinstance(event, ContextCompactionStarted) for event in events)
    assert any(isinstance(event, ContextCompactionCompleted) for event in events)


async def test_summary_agent_enforces_output_budget_and_reports_real_usage() -> None:
    observed_max_tokens: list[int | None] = []
    summary_json = (
        '{"goals":[],"constraints":[],"completed":[],"current_plan":[],'
        '"important_files":[],"key_facts":[],"failures_and_approvals":[],"outstanding":[]}'
    )

    def summarize(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        observed_max_tokens.append((info.model_settings or {}).get("max_tokens"))
        return ModelResponse(parts=[TextPart(content=summary_json)])

    manager = ContextManager(
        ContextConfig(
            enabled=True,
            soft_token_limit=10,
            keep_recent_tokens=10,
            summary_max_tokens=321,
        ),
        FunctionModel(function=summarize),
    )

    async def emit(_event: RunEvent) -> None:
        return None

    prepared = await manager.prepare(
        [ModelRequest(parts=[UserPromptPart(content="history " * 100)])],
        PlanState(),
        [],
        emit,
    )

    assert observed_max_tokens == [321]
    assert prepared.compaction is not None
    assert prepared.usage == prepared.compaction.usage
    assert prepared.usage["requests"] == 1


async def test_context_manager_counts_current_request_when_deciding_to_compact() -> None:
    history = [ModelRequest(parts=[UserPromptPart(content="h" * 200)])]
    config = ContextConfig(enabled=True, soft_token_limit=100, keep_recent_tokens=80)
    summary_json = (
        '{"goals":[],"constraints":[],"completed":[],"current_plan":[],'
        '"important_files":[],"key_facts":[],"failures_and_approvals":[],"outstanding":[]}'
    )
    manager = ContextManager(config, model=_summary_function_model(summary_json))
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    prepared = await manager.prepare(
        history,
        PlanState(),
        [],
        emit,
        reservation=ContextReservation(prompt_tokens=60),
    )

    assert prepared.compaction is not None
    assert any(isinstance(event, ContextCompactionStarted) for event in events)


async def test_context_manager_rejects_request_that_cannot_fit_without_history() -> None:
    config = ContextConfig(enabled=True, soft_token_limit=100, keep_recent_tokens=80)
    manager = ContextManager(config, model="test")  # type: ignore[arg-type]

    async def emit(_event: RunEvent) -> None:
        return None

    with pytest.raises(ContextBudgetExceeded, match="request footprint"):
        await manager.prepare(
            [],
            PlanState(),
            [],
            emit,
            reservation=ContextReservation(prompt_tokens=101),
        )


async def test_prepare_reserves_prompt_and_schema_tokens_from_budget() -> None:
    """The current prompt + tool schema must be counted against the recent-
    window budget so compaction leaves room for them, instead of filling the
    window with history and letting the new request blow the context limit."""

    # History with several turns so the window has content to trim.
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="turn one " * 400)]),
        ModelResponse(parts=[TextPart(content="reply one " * 400)]),
        ModelRequest(parts=[UserPromptPart(content="turn two " * 400)]),
        ModelResponse(parts=[TextPart(content="reply two " * 400)]),
        ModelRequest(parts=[UserPromptPart(content="turn three " * 400)]),
    ]
    config = ContextConfig(
        enabled=True, soft_token_limit=4000, keep_recent_tokens=2000, summary_max_tokens=2000
    )
    summary_yaml = (
        '{"goals":["g"],"constraints":[],"completed":[],"current_plan":[],'
        '"important_files":[],"key_facts":[],"failures_and_approvals":[],"outstanding":[]}'
    )
    manager = ContextManager(config, model=_summary_function_model(summary_yaml))
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    # Without reservation: the full recent window is kept.
    prepared_no_reserve = await manager.prepare(history, PlanState(), [], emit)
    assert prepared_no_reserve.compaction is not None
    assert estimate_message_tokens(prepared_no_reserve.history) > 0

    # With a large prompt + schema reservation, the window must shrink so the
    # total (window + prompt + schema) respects the budget.
    prepared_reserved = await manager.prepare(
        history,
        PlanState(),
        [],
        emit,
        current_prompt="a very long new prompt " * 200,
        instructions_estimate=500,
        tool_schema_estimate=1000,
    )
    assert prepared_reserved.compaction is not None
    reserve_tokens = estimate_message_tokens(prepared_reserved.history)
    # The prepared context plus the independently worked reservation (1150
    # prompt + 500 instructions + 1000 schema) fits below the 4000-token limit.
    assert reserve_tokens + 2650 <= 4000


async def test_context_manager_skips_compaction_when_under_limit() -> None:
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="short")])]
    config = ContextConfig(
        enabled=True, soft_token_limit=1_000_000, keep_recent_tokens=4000, summary_max_tokens=2000
    )
    manager = ContextManager(config, model=_summary_function_model("{}"))
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    prepared = await manager.prepare(history, PlanState(), [], emit)

    assert prepared.compaction is None
    assert prepared.history == history
    assert not any(
        isinstance(event, (ContextCompactionStarted, ContextCompactionCompleted)) for event in events
    )


async def test_context_manager_skips_compaction_when_disabled() -> None:
    history = [ModelRequest(parts=[UserPromptPart(content="x" * 4000)])]
    config = ContextConfig(
        enabled=False, soft_token_limit=10, keep_recent_tokens=1000, summary_max_tokens=2000
    )
    manager = ContextManager(config, model=_summary_function_model("{}"))
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    prepared = await manager.prepare(history, PlanState(), [], emit)

    assert prepared.compaction is None
    assert prepared.history == history


async def test_context_manager_falls_back_on_failure() -> None:
    history = [ModelRequest(parts=[UserPromptPart(content="x" * 4000)])]
    config = ContextConfig(
        enabled=True, soft_token_limit=10, keep_recent_tokens=1000, summary_max_tokens=2000
    )
    manager = ContextManager(config, model=_summary_function_model("", fail=True))
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    prepared = await manager.prepare(history, PlanState(), [], emit)

    # On failure the original history is returned and a failure event is emitted,
    # but no compaction record is produced.
    assert prepared.compaction is None
    assert prepared.history == history
    assert any(isinstance(event, ContextCompactionFailed) for event in events)


def test_compaction_record_is_frozen() -> None:
    record = CompactionRecord(
        summary=ContextSummary(
            goals=[],
            constraints=[],
            completed=[],
            current_plan=[],
            important_files=[],
            key_facts=[],
            failures_and_approvals=[],
            outstanding=[],
        ),
        active_history=[],
        source_message_count=0,
        usage={},
    )
    with pytest.raises((AttributeError, Exception)):
        record.source_message_count = 5  # type: ignore[misc]


# Unused import retained for typing parity with the runtime tests in this file.
_ = Path


# ---------------------------------------------------------------------------
# Token-budgeted cut point (retain_recent_tokens)
# ---------------------------------------------------------------------------


def test_retain_recent_tokens_keeps_within_budget() -> None:
    """The retained window fits within ``keep_tokens`` (plus one turn)."""

    # Each request is ~25 tokens (100 chars / 4). Budget 100 tokens = ~4 requests.
    history = [
        ModelRequest(parts=[UserPromptPart(content="a" * 100)]),  # 25 tok
        ModelRequest(parts=[UserPromptPart(content="b" * 100)]),
        ModelRequest(parts=[UserPromptPart(content="c" * 100)]),
        ModelRequest(parts=[UserPromptPart(content="d" * 100)]),
        ModelRequest(parts=[UserPromptPart(content="e" * 100)]),
    ]
    kept = retain_recent_tokens(history, keep_tokens=60)
    # ~60 tokens = ~2.4 requests → snaps to boundary, keeps last 2-3.
    assert len(kept) <= 3
    # Always includes the most recent message.
    assert kept[-1] is history[-1]


def test_retain_recent_tokens_snaps_to_safe_boundary() -> None:
    """The cut never orphans a tool result from its call.

    If the token budget runs out mid-turn (between a tool call and its
    result), the cut snaps forward to the next user-prompt request rather
    than splitting the pair.
    """

    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    history = [
        ModelRequest(parts=[UserPromptPart(content="first turn" * 50)]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "x"})]),
        # Turn 2: a tool call + return that must stay together.
        ModelRequest(parts=[UserPromptPart(content="second turn" * 50)]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "y"})]),
    ]
    # Add a tool return after the last call — it must not be orphaned.
    history.append(
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="data", tool_call_id="c1")])
    )
    history.append(ModelRequest(parts=[UserPromptPart(content="final turn" * 50)]))
    kept = retain_recent_tokens(history, keep_tokens=10)
    # The cut must land at a user-prompt boundary, never between the tool
    # call and its return.
    assert any(
        isinstance(p, UserPromptPart)
        for msg in kept
        for p in getattr(msg, "parts", [])
        if isinstance(msg, ModelRequest)
    )


# ---------------------------------------------------------------------------
# Per-tool-result truncation at summary time
# ---------------------------------------------------------------------------


def test_serialize_for_summary_truncates_each_tool_result() -> None:
    """Each tool result is capped independently; one giant result can't crowd out others.

    The old global ``[: N]`` prefix slice dropped the most recent (most
    relevant) turns once the budget filled. Per-result truncation keeps every
    turn's first N chars visible to the summarizer.
    """

    from pydantic_ai.messages import ToolReturnPart

    config = ContextConfig(
        enabled=True,
        soft_token_limit=100,
        keep_recent_tokens=1000,
        summary_tool_result_chars=100,
        summary_max_tokens=2000,
    )
    manager = ContextManager(config, model="test")  # type: ignore[arg-type]
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(content="q"),
                ToolReturnPart(tool_name="big", content="X" * 5000, tool_call_id="c1"),
                ToolReturnPart(tool_name="small", content="ok", tool_call_id="c2"),
            ]
        ),
    ]
    rendered = manager._serialize_for_summary(history)  # type: ignore[reportPrivateUsage]
    # The big result is capped to 100 chars + truncation notice; the small
    # one is preserved fully. Both appear in the output.
    assert "[... 4900 more chars truncated]" in rendered
    assert "ok" in rendered
    # The big result's full 5000 chars are NOT in the rendered output.
    assert "X" * 5000 not in rendered


# ---------------------------------------------------------------------------
# Iterative summary update
# ---------------------------------------------------------------------------


def test_summary_instructions_includes_previous_summary_when_given() -> None:
    """When ``previous_summary`` is passed, the prompt instructs an UPDATE not a rebuild."""

    config = ContextConfig(enabled=True, soft_token_limit=10, keep_recent_tokens=1000)
    manager = ContextManager(config, model="test")  # type: ignore[arg-type]
    prior = ContextSummary(goals=["original goal"], completed=["step 1"])
    instructions = manager._summary_instructions(  # type: ignore[reportPrivateUsage]
        plan=PlanState(),
        diagnostics=[],
        previous_summary=prior,
    )
    assert "UPDATE" in instructions
    assert "PRESERVE" in instructions
    assert "original goal" in instructions


def test_summary_instructions_omits_previous_section_on_first_run() -> None:
    """Without a prior summary, the prompt is the plain from-scratch variant."""

    config = ContextConfig(enabled=True, soft_token_limit=10, keep_recent_tokens=1000)
    manager = ContextManager(config, model="test")  # type: ignore[arg-type]
    instructions = manager._summary_instructions(  # type: ignore[reportPrivateUsage]
        plan=PlanState(),
        diagnostics=[],
        previous_summary=None,
    )
    assert "UPDATE" not in instructions
    assert "<previous-summary>" not in instructions


# ---------------------------------------------------------------------------
# Compaction result invariants (Phase 1.5 safety cutpoint)
# ---------------------------------------------------------------------------


def test_validate_active_history_rejects_orphaned_tool_return() -> None:
    """A window starting with a ToolReturnPart (no preceding call) is invalid."""
    from pydantic_ai.messages import ToolReturnPart

    from lumen.context import validate_active_history

    history = [
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="x", tool_call_id="c1")]),
    ]
    errors = validate_active_history(history)
    assert any("ToolReturnPart" in e or "does not start" in e for e in errors)


def test_validate_active_history_accepts_summary_prefix_then_user_request() -> None:
    """The summary-prefix (SystemPromptPart-only request) is skipped, and the
    first real conversational message must be a user request."""

    from lumen.context import validate_active_history

    history = [
        ModelRequest(parts=[SystemPromptPart(content="summary")]),
        ModelRequest(parts=[UserPromptPart(content="what files exist?")]),
        ModelResponse(parts=[TextPart(content="here they are")]),
    ]
    assert validate_active_history(history) == []


def test_validate_active_history_rejects_missing_call_for_return() -> None:
    """Every ToolReturnPart must have a matching preceding ToolCallPart."""
    from pydantic_ai.messages import ToolReturnPart

    from lumen.context import validate_active_history

    history = [
        ModelRequest(parts=[UserPromptPart(content="read x")]),
        # A return with id c2 but no matching call for c2.
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="x", tool_call_id="c2")]),
    ]
    errors = validate_active_history(history)
    assert any("c2" in e for e in errors)


def test_validate_active_history_accepts_matched_call_and_return() -> None:
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    from lumen.context import validate_active_history

    history = [
        ModelRequest(parts=[UserPromptPart(content="read x")]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="x", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    assert validate_active_history(history) == []


def test_retain_recent_tokens_result_passes_invariants() -> None:
    """The token cut's output must always satisfy the structural invariants."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    from lumen.context import validate_active_history

    history = [
        ModelRequest(parts=[UserPromptPart(content="turn one " * 200)]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "a"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="a", tool_call_id="c1")]),
        ModelRequest(parts=[UserPromptPart(content="turn two " * 200)]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "b"}, tool_call_id="c2")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="b", tool_call_id="c2")]),
        ModelRequest(parts=[UserPromptPart(content="turn three " * 200)]),
    ]
    kept = retain_recent_tokens(history, keep_tokens=50)
    assert validate_active_history(kept) == []


def test_build_active_result_passes_invariants() -> None:
    """The full _build_active output (summary prefix + retained window) must
    satisfy the invariants: the prefix is skipped, the first real message is a
    user request, and no tool result is orphaned."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    from lumen.context import validate_active_history

    config = ContextConfig(enabled=True, soft_token_limit=10, keep_recent_tokens=1000)
    manager = ContextManager(config, model="test")  # type: ignore[arg-type]
    summary = ContextSummary(goals=["find the file"])
    history = [
        ModelRequest(parts=[UserPromptPart(content="turn one " * 200)]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "a"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="a", tool_call_id="c1")]),
        ModelRequest(parts=[UserPromptPart(content="turn two " * 200)]),
    ]
    active = manager._build_active(history, summary)  # type: ignore[reportPrivateUsage]
    assert validate_active_history(active) == []


def test_request_budget_estimator_counts_complete_tool_schema() -> None:
    estimator = RequestBudgetEstimator()
    small = estimator.estimate_tool_schemas(
        [{"name": "lookup", "description": "find", "parameters": {"type": "object"}}]
    )
    complex_schema = estimator.estimate_tool_schemas(
        [
            {
                "name": "lookup",
                "description": "find a customer and include every matching attribute",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "full text search query"},
                        "filters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            }
        ]
    )

    assert complex_schema > small
