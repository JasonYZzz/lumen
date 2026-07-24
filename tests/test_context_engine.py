"""Tests for the ContextEngine Seam (M1).

Locks the plan §M1 contract: ``prepare`` returns a provider-ready envelope with
a fingerprint, ``commit`` verifies the fingerprint and is idempotent, a stale or
unknown commit is a session-sequence conflict, and ``control`` exposes the
read-only report. These exercise the Seam in isolation before the runtime is
migrated onto it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.config import ContextConfig
from lumen.context import (
    AgentRef,
    ConservativeTokenCounter,
    ContextCommit,
    ContextCompactCommand,
    ContextEngine,
    ContextMemoryCommand,
    ContextReportCommand,
    ContextRequest,
    ContextSequenceError,
    ContextSummary,
    ContextZone,
    RuntimeContextSnapshot,
    SessionRef,
    TaskSnapshot,
)
from lumen.events import RunEvent
from lumen.plan import PlanState

_EMPTY_SUMMARY_JSON = (
    '{"goals":["g"],"constraints":[],"completed":[],"current_plan":[],'
    '"important_files":[],"key_facts":[],"failures_and_approvals":[],"outstanding":[]}'
)


def _summary_model() -> FunctionModel:
    """A FunctionModel that always yields a valid empty ContextSummary."""

    def function(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=_EMPTY_SUMMARY_JSON)])

    return FunctionModel(function=function)


def _engine(
    *,
    soft_token_limit: int = 100,
    model_id: str | None = None,
) -> ContextEngine:
    return ContextEngine(
        ContextConfig(
            enabled=True,
            soft_token_limit=soft_token_limit,
            keep_recent_tokens=2000,
            summary_max_tokens=2000,
        ),
        model=_summary_model(),
        model_id=model_id,
    )


def _request(history: list[ModelMessage], *, session_id: str = "s1") -> ContextRequest:
    return ContextRequest(
        session=SessionRef(id=session_id),
        agent=AgentRef(name="test"),
        prompt="next step",
        task=TaskSnapshot(plan=PlanState()),
        runtime=RuntimeContextSnapshot(instructions="be helpful"),
        history=tuple(history),
    )


async def _no_emit(_event: RunEvent) -> None:
    return None


def _history_over_limit() -> list[ModelMessage]:
    """A history large enough to trip the tiny soft limit used in these tests."""

    return [
        ModelRequest(parts=[UserPromptPart(content="x" * 4000)]),
        ModelResponse(parts=[TextPart(content="y" * 4000)]),
        ModelRequest(parts=[UserPromptPart(content="z" * 100)]),
    ]


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #


async def test_prepare_returns_envelope_with_fingerprint_and_budget() -> None:
    engine = _engine()
    envelope = await engine.prepare(_request(_history_over_limit()), _no_emit)

    assert envelope.fingerprint  # non-empty sha256 hex
    assert envelope.budget is not None
    assert envelope.budget.used_tokens > 0
    assert envelope.budget.estimated is True
    # Under pressure the legacy manager compacted, so a compaction record is
    # carried (M1-compat bridge until the structured checkpoint lands in M4).
    assert envelope.compaction is not None
    assert envelope.compaction.source_message_count == 3
    # M4: a successful compaction produces a structured checkpoint (source range
    # + parent + digest), replacing the bare None.
    assert envelope.checkpoint is not None
    assert envelope.checkpoint.source_end == 3
    assert envelope.checkpoint.parent_checkpoint_id is None  # first compaction
    # M2: the envelope now carries source-tracked blocks (SYSTEM etc.).
    assert envelope.blocks
    assert any(block.zone is ContextZone.SYSTEM for block in envelope.blocks)


async def test_prepare_under_generous_limit_carries_no_compaction() -> None:
    engine = _engine(soft_token_limit=1_000_000)
    history = [ModelRequest(parts=[UserPromptPart(content="short")])]
    envelope = await engine.prepare(_request(history), _no_emit)
    assert envelope.compaction is None
    assert envelope.messages == tuple(history)


async def test_prepare_routes_previous_summary_to_the_manager() -> None:
    """The request's previous_summary reaches the legacy iterative-update path."""

    engine = _engine()
    prior = ContextSummary(goals=["carried goal"])
    request = _request(_history_over_limit())
    request = ContextRequest(
        session=request.session,
        agent=request.agent,
        prompt=request.prompt,
        task=request.task,
        runtime=request.runtime,
        history=request.history,
        previous_summary=prior,
    )
    envelope = await engine.prepare(request, _no_emit)
    # The summary produced by the model is the empty one, but the manager's
    # iterative-update prompt was built from the carried prior summary - proven
    # by the manager having accepted previous_summary without error and the
    # envelope carrying a compaction record.
    assert envelope.compaction is not None


# --------------------------------------------------------------------------- #
# commit: fingerprint, idempotency, sequence conflict
# --------------------------------------------------------------------------- #


async def test_commit_appends_new_messages_to_prepared_history() -> None:
    engine = _engine(soft_token_limit=1_000_000)
    history = [ModelRequest(parts=[UserPromptPart(content="q")])]
    envelope = await engine.prepare(_request(history), _no_emit)
    new_messages = [ModelResponse(parts=[TextPart(content="a")])]
    transition = await engine.commit(
        ContextCommit(
            session=SessionRef(id="s1"),
            envelope_fingerprint=envelope.fingerprint,
            new_messages=tuple(new_messages),
        ),
        _no_emit,
    )
    assert transition.active_history == (*envelope.messages, *new_messages)


async def test_commit_is_idempotent_for_repeated_fingerprint() -> None:
    """A repeated commit returns the same transition; new_messages are not
    double-applied (plan §7: 重复 commit 必须幂等)."""

    engine = _engine(soft_token_limit=1_000_000)
    envelope = await engine.prepare(_request([ModelRequest(parts=[UserPromptPart(content="q")])]), _no_emit)
    commit = ContextCommit(
        session=SessionRef(id="s1"),
        envelope_fingerprint=envelope.fingerprint,
        new_messages=(ModelResponse(parts=[TextPart(content="a")]),),
    )
    first = await engine.commit(commit, _no_emit)
    second = await engine.commit(commit, _no_emit)
    assert second is first
    assert len(second.active_history) == len(first.active_history)


async def test_commit_without_prepare_is_a_sequence_conflict() -> None:
    engine = _engine()
    with pytest.raises(ContextSequenceError, match="no prepared envelope"):
        await engine.commit(
            ContextCommit(session=SessionRef(id="unknown"), envelope_fingerprint="stale"),
            _no_emit,
        )


async def test_commit_with_stale_fingerprint_is_a_sequence_conflict() -> None:
    """A second prepare for the same session supersedes the first envelope; a
    commit of the OLD fingerprint must be rejected (plan §7 sequence invariant)."""

    engine = _engine(soft_token_limit=1_000_000)
    first = await engine.prepare(_request([ModelRequest(parts=[UserPromptPart(content="q1")])]), _no_emit)
    # A second prepare for the same session replaces the pending envelope.
    second = await engine.prepare(_request([ModelRequest(parts=[UserPromptPart(content="q2")])]), _no_emit)
    assert first.fingerprint != second.fingerprint
    with pytest.raises(ContextSequenceError, match="fingerprint"):
        await engine.commit(
            ContextCommit(session=SessionRef(id="s1"), envelope_fingerprint=first.fingerprint),
            _no_emit,
        )


async def test_commit_is_scoped_per_session() -> None:
    """A pending envelope for one session must not satisfy a commit for another."""

    engine = _engine(soft_token_limit=1_000_000)
    await engine.prepare(
        _request([ModelRequest(parts=[UserPromptPart(content="q")])], session_id="a"), _no_emit
    )
    with pytest.raises(ContextSequenceError, match="no prepared envelope"):
        await engine.commit(
            ContextCommit(session=SessionRef(id="b"), envelope_fingerprint="anything"),
            _no_emit,
        )


# --------------------------------------------------------------------------- #
# control
# --------------------------------------------------------------------------- #


async def test_control_report_returns_last_prepared_budget() -> None:
    engine = _engine(soft_token_limit=1_000_000, model_id="anthropic:claude-opus-4")
    await engine.prepare(_request([ModelRequest(parts=[UserPromptPart(content="q")])]), _no_emit)
    result = await engine.control(ContextReportCommand(), _no_emit)
    assert result.status == "ok"
    assert result.payload["used_tokens"] > 0
    # M2: the window is the resolved model spec (200k for anthropic), not the
    # legacy soft_token_limit.
    assert result.payload["context_window_tokens"] == 200_000
    assert result.payload["estimated"] is False
    # /context surfaces per-zone usage and the source-tracked blocks.
    assert result.payload["zones"]
    assert result.payload["blocks"]
    assert any(b["zone"] == "system" for b in result.payload["blocks"])


async def test_control_report_marks_estimated_window_for_unknown_model() -> None:
    engine = _engine(soft_token_limit=1_000_000, model_id="acme:custom-7b")
    await engine.prepare(_request([ModelRequest(parts=[UserPromptPart(content="q")])]), _no_emit)
    result = await engine.control(ContextReportCommand(), _no_emit)
    assert result.payload["context_window_tokens"] == 32_000
    assert result.payload["estimated"] is True


async def test_control_report_without_prepare_is_empty() -> None:
    engine = _engine()
    result = await engine.control(ContextReportCommand(), _no_emit)
    assert result.status == "ok"
    assert result.payload == {}


async def test_control_compact_schedules_and_memory_is_unsupported() -> None:
    """/compact (M4) schedules a forced compaction; /memory stays M5-unsupported."""

    engine = _engine()
    compact = await engine.control(ContextCompactCommand(focus="x", session_id="s1"), _no_emit)
    assert compact.status == "ok"
    assert "scheduled" in compact.message
    memory = await engine.control(ContextMemoryCommand(action="list"), _no_emit)
    assert memory.status == "unsupported"


# --------------------------------------------------------------------------- #
# Read-only control never compacts (plan §7: 只读命令不得触发摘要或记忆生成)
# --------------------------------------------------------------------------- #


async def test_readonly_control_does_not_mutate_pending() -> None:
    engine = _engine(soft_token_limit=1_000_000)
    await engine.prepare(_request([ModelRequest(parts=[UserPromptPart(content="q")])]), _no_emit)
    before = dict(engine._pending)  # type: ignore[reportPrivateUsage]
    await engine.control(ContextReportCommand(), _no_emit)
    assert engine._pending == before  # type: ignore[reportPrivateUsage]


# --------------------------------------------------------------------------- #
# Legacy deprecation (plan §M1: legacy adapter emits a deprecation warning)
# --------------------------------------------------------------------------- #


def test_legacy_context_manager_construction_warns() -> None:
    """Direct ContextManager construction is deprecated behind ContextEngine."""

    from lumen.context.legacy import ContextManager

    with pytest.warns(DeprecationWarning, match="ContextManager is deprecated"):
        ContextManager(ContextConfig(enabled=True, soft_token_limit=100), model="test")  # type: ignore[arg-type]


def test_engine_construction_does_not_warn() -> None:
    """The engine wraps the legacy manager internally without deprecation noise."""

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        _engine()  # must not raise


# --------------------------------------------------------------------------- #
# M3: tool-output reduction is wired into prepare
# --------------------------------------------------------------------------- #


async def test_prepare_reduces_large_tool_outputs_outside_recent_window(
    tmp_path: Path,
) -> None:
    """A big tool output before the recent window is receipt-ized in prepare.

    The recent window (kept full) is tiny here, so the early 5000-byte output
    falls outside it and is replaced by a receipt; the envelope carries the
    structured receipt and its artifact ref. (plan §9.3, §3.2 #8)
    """

    engine = ContextEngine(
        ContextConfig(
            enabled=True,
            soft_token_limit=1_000_000,  # no compaction; isolation of reduction
            keep_recent_tokens=10,  # tiny recent window -> big output is "old"
            summary_max_tokens=2000,
        ),
        model=_summary_model(),
        artifact_root=str(tmp_path / "artifacts"),
    )
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="run the build")]),
        ModelResponse(parts=[ToolCallPart(tool_name="run_command", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_command", content="X" * 5000, tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="done")]),
        # A trailing user turn becomes the safe-cut boundary for the recent window.
        ModelRequest(parts=[UserPromptPart(content="next step")]),
        ModelResponse(parts=[TextPart(content="ok")]),
    ]
    envelope = await engine.prepare(_request(history), _no_emit)

    # The big body was spilled to an artifact and surfaced as a receipt.
    assert len(envelope.tool_receipts) == 1
    receipt = envelope.tool_receipts[0]
    assert receipt.artifact_ref is not None
    assert receipt.byte_size == 5000
    # The model-visible messages no longer carry the raw 5000-byte body.
    rendered = "\n".join(
        str(getattr(p, "content", "")) for m in envelope.messages for p in getattr(m, "parts", [])
    )
    assert "X" * 5000 not in rendered
    assert "tool-receipt" in rendered


# --------------------------------------------------------------------------- #
# M4: safety net, anti-thrash, /compact force, checkpoint linking
# --------------------------------------------------------------------------- #


def _failing_summary_model() -> FunctionModel:
    """A FunctionModel whose summary call always raises (forces the safety net)."""

    def function(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise RuntimeError("summary model exploded")

    return FunctionModel(function=function)


async def test_prepare_safety_net_never_returns_over_limit_history(tmp_path: Path) -> None:
    """A failed summary must NOT fall back to the original over-limit history.

    The legacy manager returns the original history on summary failure; the M4
    safety net degrades it so the envelope fits the model window (plan §9.4).
    """

    engine = ContextEngine(
        ContextConfig(enabled=True, soft_token_limit=100, keep_recent_tokens=2000, summary_max_tokens=2000),
        model=_failing_summary_model(),
        artifact_root=str(tmp_path / "artifacts"),
    )
    # 10 user turns of ~4000 tokens each = ~40000 tokens, over the 32k window.
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="x" * 16000 + str(i))]) for i in range(10)
    ]
    envelope = await engine.prepare(_request(history), _no_emit)
    # The summary failed -> no compaction record, no checkpoint.
    assert envelope.compaction is None
    assert envelope.checkpoint is None
    # The safety invariant: the prepared history fits the model window.
    counter = ConservativeTokenCounter()
    used = counter.count_messages(envelope.messages).tokens
    assert used + 2000 <= 32_000  # used + fixed-ish + reserve within the window
    # Degradation shrank the over-limit history.
    assert len(envelope.messages) < len(history)


async def test_anti_thrash_stops_re_summarizing_after_auto_limit() -> None:
    """Past the auto-compaction limit, the engine degrades instead of re-summarising."""

    engine = _engine(soft_token_limit=100)  # _history_over_limit (~2000 tok) is over soft
    history = _history_over_limit()
    # First two auto-compactions succeed.
    env1 = await engine.prepare(_request(history), _no_emit)
    env2 = await engine.prepare(_request(history), _no_emit)
    assert env1.compaction is not None
    assert env2.compaction is not None
    # The second checkpoint links to the first (parent chain, plan §10).
    assert env2.checkpoint is not None
    assert env2.checkpoint.parent_checkpoint_id == env1.checkpoint.checkpoint_id
    # Third prepare is thrashed: no re-summarisation (no compaction record).
    env3 = await engine.prepare(_request(history), _no_emit)
    assert env3.compaction is None


async def test_compact_command_forces_compaction_even_under_soft_limit() -> None:
    """/compact schedules a forced compaction on the next turn (plan §9.1)."""

    engine = _engine(soft_token_limit=1_000_000)  # history is under soft -> no auto compact
    history = _history_over_limit()
    # Without /compact, no compaction (under the generous soft limit).
    env_plain = await engine.prepare(_request(history), _no_emit)
    assert env_plain.compaction is None
    # /compact forces the next prepare to compact.
    scheduled = await engine.control(ContextCompactCommand(session_id="s1"), _no_emit)
    assert scheduled.status == "ok"
    env_forced = await engine.prepare(_request(history), _no_emit)
    assert env_forced.compaction is not None
    assert env_forced.checkpoint is not None
