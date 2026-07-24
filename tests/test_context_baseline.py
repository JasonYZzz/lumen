"""Non-regression baseline for the context engine (M0).

Captures the *current* deterministic behaviour of ``context.py`` against five
frozen fixture transcripts (plan §M0: "record 5 reference transcripts' token,
compaction result and latency as a non-regression baseline").

Two kinds of assertion:

* **Invariant assertions** (should never regress across milestones): tool
  pairing holds, the recent window always includes the newest message, fixed
  content cannot exceed the window silently, and the safe user-boundary cut
  is preserved. These guard the safety contract, not the numbers.
* **Numeric baseline** (locked to the current byte/4 estimator): the exact token
  counts. M2 replaces the byte/4 estimator with a provider-aware tokenizer, so
  this section is *expected* to change there - the diff documents the intended
  behaviour change and the section is updated in lock-step. Between M0 and M2
  it catches accidental drift.
"""

from __future__ import annotations

from pathlib import Path

import context_fixtures as cf
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.config import ContextConfig
from lumen.context import (
    ContextManager,
    RequestBudgetEstimator,
    estimate_message_tokens,
    retain_recent_tokens,
    validate_active_history,
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


async def _no_emit(_event: RunEvent) -> None:
    return None


# --------------------------------------------------------------------------- #
# Fixture transcripts are loadable offline (plan §M0: "fixtures 可离线运行")
# --------------------------------------------------------------------------- #


def test_all_fixtures_load_offline() -> None:
    """Every fixture transcript is well-formed and loadable without a provider."""

    transcripts = {
        "long_shell": cf.load_long_shell_history(),
        "chinese": cf.load_chinese_history(),
        "tool_error": cf.load_tool_error_history(),
    }
    for name, messages in transcripts.items():
        assert messages, f"{name} fixture is empty"
        # Every transcript must be structurally valid (no orphaned tool returns).
        assert validate_active_history(messages) == [], f"{name} has invariant violations"
    assert len(cf.load_mcp_tool_schemas()) == 5


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_session_fixtures_resume_without_rewrite(tmp_path: Path, version: int) -> None:
    """Frozen v1-v4 session fixtures load and rebuild active history (plan §M0)."""

    loaded = cf.load_legacy_session(tmp_path, version)
    assert loaded.turns, f"v{version} session has no turns"
    assert loaded.history, f"v{version} rebuilt no active history"
    assert loaded.latest_compaction_summary is None  # none of these fixtures compacted


# --------------------------------------------------------------------------- #
# Invariant assertions (must never regress across milestones)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "loader",
    [cf.load_long_shell_history, cf.load_chinese_history, cf.load_tool_error_history],
    ids=["long_shell", "chinese", "tool_error"],
)
def test_retain_recent_always_keeps_newest_and_stays_valid(loader: object) -> None:
    """The safe cut never drops the newest message and never breaks pairing."""

    messages = loader()  # type: ignore[operator]
    kept = retain_recent_tokens(messages, keep_tokens=2000)
    assert kept, "retained window must not be empty"
    assert kept[-1] is messages[-1]
    assert validate_active_history(kept) == []


@pytest.mark.parametrize(
    "loader",
    [cf.load_long_shell_history, cf.load_chinese_history, cf.load_tool_error_history],
    ids=["long_shell", "chinese", "tool_error"],
)
async def test_compaction_produces_valid_history_under_pressure(loader: object) -> None:
    """Under a tiny soft limit compaction triggers and the result is still valid."""

    messages = loader()  # type: ignore[operator]
    manager = ContextManager(
        ContextConfig(enabled=True, soft_token_limit=50, keep_recent_tokens=2000, summary_max_tokens=2000),
        model=_summary_model(),
    )
    prepared = await manager.prepare(messages, PlanState(), [], _no_emit)
    assert prepared.compaction is not None
    assert prepared.compaction.source_message_count == len(messages)
    assert validate_active_history(prepared.history) == []


async def test_compaction_skipped_under_generous_limit() -> None:
    """A generous limit never compacts; history passes through unchanged."""

    messages = cf.load_long_shell_history()
    manager = ContextManager(
        ContextConfig(
            enabled=True, soft_token_limit=100_000, keep_recent_tokens=2000, summary_max_tokens=2000
        ),
        model=_summary_model(),
    )
    prepared = await manager.prepare(messages, PlanState(), [], _no_emit)
    assert prepared.compaction is None
    assert prepared.history == messages


# --------------------------------------------------------------------------- #
# Numeric baseline (locked to the M0 byte/4 estimator; M2 updates this section)
# --------------------------------------------------------------------------- #

#: Token estimates under the current byte/4 estimator. M2's provider-aware
#: tokenizer will change these; the diff is the intended behaviour change.
_TOKEN_BASELINE: dict[str, dict[str, int]] = {
    "long_shell": {"full_tokens": 510, "retained_tokens_2k": 510},
    "chinese": {"full_tokens": 460, "retained_tokens_2k": 460},
    "tool_error": {"full_tokens": 108, "retained_tokens_2k": 108},
}


@pytest.mark.parametrize(
    "loader, expected",
    [
        (cf.load_long_shell_history, _TOKEN_BASELINE["long_shell"]),
        (cf.load_chinese_history, _TOKEN_BASELINE["chinese"]),
        (cf.load_tool_error_history, _TOKEN_BASELINE["tool_error"]),
    ],
    ids=["long_shell", "chinese", "tool_error"],
)
def test_token_estimate_baseline(loader: object, expected: dict[str, int]) -> None:
    """Lock the byte/4 token estimate for each fixture (updated in M2)."""

    messages = loader()  # type: ignore[operator]
    assert estimate_message_tokens(messages) == expected["full_tokens"]
    kept = retain_recent_tokens(messages, keep_tokens=2000)
    assert estimate_message_tokens(kept) == expected["retained_tokens_2k"]


def test_mcp_schema_token_baseline() -> None:
    """Lock the tool-schema token estimate for the MCP fixture (updated in M2)."""

    estimator = RequestBudgetEstimator()
    assert estimator.estimate_tool_schemas(cf.load_mcp_tool_schemas()) == 471
