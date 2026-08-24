"""Tests for safe compaction: thresholds, anti-thrash, degradation (M4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from lumen.context.artifacts import ArtifactStore
from lumen.context.budget import ConservativeTokenCounter
from lumen.context.compaction import (
    CompactionPolicy,
    CompactionThrashState,
    FixedContextTooLarge,
    Thresholds,
    degrade_to_window,
)
from lumen.context.legacy import validate_active_history


def _policy() -> CompactionPolicy:
    return CompactionPolicy(
        soft_ratio=0.80, hard_ratio=0.92, target_ratio=0.55, max_auto_compactions=2, anti_thrash_turns=5
    )


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


def test_thresholds_derive_from_window() -> None:
    thresholds = Thresholds.for_window(100_000, _policy())
    assert thresholds.soft == 80_000
    assert thresholds.hard == 92_000
    assert thresholds.target == 55_000
    assert thresholds.emergency_reserve == 5_000


def test_thrash_window_expires_after_anti_thrash_turns() -> None:
    state = CompactionThrashState(anti_thrash_turns=5)
    state.record_auto_compaction()
    state.record_auto_compaction()
    assert state.thrashed(_policy())
    for _ in range(5):
        state.advance_turn()
    assert not state.thrashed(_policy())  # window expired, count reset


# --------------------------------------------------------------------------- #
# degrade_to_window: the safety invariant (never return over-limit history)
# --------------------------------------------------------------------------- #


def _alternating_history(turns: int, *, chars: int = 400) -> list[ModelMessage]:
    """turns of user/assistant, each message ~chars bytes (~chars/4 tokens)."""

    messages: list[ModelMessage] = []
    for number in range(turns):
        messages.append(ModelRequest(parts=[UserPromptPart(content="x" * chars + str(number))]))
        messages.append(ModelResponse(parts=[TextPart(content="y" * chars)]))
    return messages


def test_degrade_returns_history_that_fits_the_window(tmp_path: Path) -> None:
    """The degraded history + fixed + reserve never exceeds the window."""

    counter = ConservativeTokenCounter()
    store = ArtifactStore(tmp_path / "artifacts", inline_threshold_bytes=10_000)
    history = _alternating_history(10, chars=400)  # ~10 * 2 * 100 = 2000 tokens
    window = 1000
    fixed = 100
    reserve = 50
    degraded = degrade_to_window(
        history,
        window_tokens=window,
        fixed_tokens=fixed,
        output_reserve=reserve,
        counter=counter,
        store=store,
        keep_recent_tokens=2000,
    )
    # The safety invariant: degraded + fixed + reserve fits the window.
    used = counter.count_messages(degraded).tokens
    assert used + fixed + reserve <= window
    # Degradation shrank the history.
    assert len(degraded) < len(history)
    # The result is still a structurally valid window (safe user boundary).
    assert validate_active_history(degraded) == []


def test_degrade_raises_when_fixed_prefix_alone_exceeds_window(tmp_path: Path) -> None:
    """If fixed + reserve > window, degrade raises instead of returning over-limit."""

    counter = ConservativeTokenCounter()
    with pytest.raises(FixedContextTooLarge, match="fixed context footprint"):
        degrade_to_window(
            _alternating_history(3),
            window_tokens=1000,
            fixed_tokens=960,
            output_reserve=50,  # 960 + 50 > 1000
            counter=counter,
            store=None,
            keep_recent_tokens=2000,
        )


def test_degrade_raises_when_history_cannot_be_reduced_enough(tmp_path: Path) -> None:
    """A single huge turn that still exceeds the budget after trimming raises."""

    counter = ConservativeTokenCounter()
    store = ArtifactStore(tmp_path / "artifacts", inline_threshold_bytes=10_000)
    # One user turn whose body alone exceeds the post-fixed budget.
    huge = ModelRequest(parts=[UserPromptPart(content="z" * 100_000)])
    with pytest.raises(FixedContextTooLarge):
        degrade_to_window(
            [huge],
            window_tokens=1000,
            fixed_tokens=100,
            output_reserve=50,
            counter=counter,
            store=store,
            keep_recent_tokens=2000,
        )


def test_degrade_receiptizes_tool_outputs_first(tmp_path: Path) -> None:
    """Step 1 spills bulky tool outputs to artifacts before shrinking the window."""

    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    counter = ConservativeTokenCounter()
    store = ArtifactStore(tmp_path / "artifacts", inline_threshold_bytes=100)
    history = [
        ModelRequest(parts=[UserPromptPart(content="run it")]),
        ModelResponse(parts=[ToolCallPart(tool_name="run_command", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_command", content="X" * 5000, tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="done")]),
        ModelRequest(parts=[UserPromptPart(content="next")]),
        ModelResponse(parts=[TextPart(content="ok")]),
    ]
    degraded = degrade_to_window(
        history,
        window_tokens=1000,
        fixed_tokens=50,
        output_reserve=50,
        counter=counter,
        store=store,
        keep_recent_tokens=2000,
    )
    used = counter.count_messages(degraded).tokens
    assert used + 50 + 50 <= 1000
    # The 5000-byte body is gone from the model-visible history.
    rendered = "\n".join(str(getattr(p, "content", "")) for m in degraded for p in getattr(m, "parts", []))
    assert "X" * 5000 not in rendered
    assert validate_active_history(degraded) == []
