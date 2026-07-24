"""Round-trip and validation tests for the M0 context data contracts.

The M0 acceptance criterion is "all schemas stably round-trip": every contract
type must survive ``model_dump(mode="json")`` -> ``model_validate`` without loss,
and must enforce its declared invariants. These tests prove the contract before
any engine behaviour depends on it (M1+).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lumen.context_contract import (
    CheckpointItem,
    CompactionCheckpointV1,
    ContextBlock,
    ContextBudgetReport,
    ContextPayload,
    ContextSource,
    ContextZone,
    EvidenceRef,
    ExactLiteral,
    ExecutionState,
    FileState,
    MemoryCandidateRef,
    ModelContextSpec,
    ObservationState,
    PressureItem,
    RetentionPolicy,
    SourceKind,
    ToolReceipt,
    TrustLevel,
    ZoneUsage,
)


def _full_checkpoint() -> CompactionCheckpointV1:
    """A checkpoint populated in every section, so round-trip covers all fields."""

    return CompactionCheckpointV1(
        checkpoint_id="cp-1",
        parent_checkpoint_id="cp-0",
        source_start=10,
        source_end=40,
        source_digest="sha256:abc",
        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        focus="refactor auth module",
        objective="land the new context engine",
        constraints=(
            CheckpointItem(id="c1", text="do not break resume", confidence=0.9, source_event_ids=("e1",)),
        ),
        decisions=(CheckpointItem(id="d1", text="use SQLite for memory", confidence=0.8),),
        plan=(CheckpointItem(id="p1", text="M0 contracts", status="completed"),),
        completed=(CheckpointItem(id="done1", text="schemas defined"),),
        files=(FileState(path="src/lumen/context.py", state=ObservationState.OBSERVED),),
        commands_and_tests=(
            ExecutionState(command="uv run pytest", exit_code=0, state=ObservationState.OBSERVED),
        ),
        failures=(CheckpointItem(id="f1", text="summary model timed out", confidence=0.7),),
        approvals=(CheckpointItem(id="a1", text="write approved"),),
        open_questions=(CheckpointItem(id="q1", text="which tokenizer?"),),
        next_actions=(CheckpointItem(id="n1", text="wire ContextEngine"),),
        exact_literals=(
            ExactLiteral(kind="path", value="src/lumen/context_contract.py"),
            ExactLiteral(kind="error_code", value="ECONNRESET"),
        ),
        uncertainties=(CheckpointItem(id="u1", text="token estimate may drift"),),
        do_not_repeat=(CheckpointItem(id="r1", text="do not re-summarise old checkpoint"),),
        memory_candidates=(
            MemoryCandidateRef(content="user prefers 2-space indent", scope="user", confidence=0.6),
        ),
    )


def _full_block() -> ContextBlock:
    return ContextBlock(
        id="block-sys-1",
        zone=ContextZone.SYSTEM,
        source=ContextSource(kind=SourceKind.SYSTEM, origin="runtime:base", revision="v1"),
        payload=ContextPayload(text="You are Lumen.", structured={"role": "assistant"}),
        token_estimate=12,
        priority=100,
        retention=RetentionPolicy.PINNED,
        trust=TrustLevel.SYSTEM,
        cache_key="system:base:v1",
        provenance=(EvidenceRef(session_id="s1", event_id="e0", detail="reinjected"),),
    )


def _full_budget() -> ContextBudgetReport:
    return ContextBudgetReport(
        context_window_tokens=128_000,
        used_tokens=71_240,
        output_reserve_tokens=16_384,
        soft_threshold_tokens=102_400,
        hard_threshold_tokens=117_760,
        target_tokens=70_400,
        estimated=False,
        zones=(
            ZoneUsage(zone=ContextZone.SYSTEM, tokens=8210, share=0.064, survival=RetentionPolicy.REINJECT),
            ZoneUsage(
                zone=ContextZone.RECENT_HISTORY, tokens=30440, share=0.238, survival=RetentionPolicy.PINNED
            ),
        ),
        pressure=(PressureItem(label="MCP schemas", tokens=7200, source="mcp:filesystem"),),
    )


def _full_receipt() -> ToolReceipt:
    return ToolReceipt(
        tool_call_id="call_1",
        tool_name="run_command",
        status="success",
        summary="listed 42 files",
        head="file_a\nfile_b",
        tail="file_z",
        byte_size=12_000,
        sha256="deadbeef",
        artifact_ref="sha256:deadbeef",
        source_event_ids=("e1", "e2"),
    )


def _full_model_spec() -> ModelContextSpec:
    return ModelContextSpec(
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        tokenizer="provider:anthropic",
        supports_remote_compaction=True,
        supports_tool_search=False,
    )


#: Every contract type with a fully-populated exemplar, exercised by the
#: parametrised round-trip and immutability tests below.
_EXEMPLARS: dict[str, object] = {
    "checkpoint": _full_checkpoint(),
    "block": _full_block(),
    "budget": _full_budget(),
    "receipt": _full_receipt(),
    "model_spec": _full_model_spec(),
}


@pytest.mark.parametrize("name", list(_EXEMPLARS))
def test_contract_round_trips_through_json(name: str) -> None:
    """model_dump(json) -> model_validate yields an equal instance (no loss)."""

    original = _EXEMPLARS[name]
    dumped = original.model_dump(mode="json")  # type: ignore[attr-defined]
    restored = type(original).model_validate(dumped)  # type: ignore[attr-defined]
    assert restored == original


@pytest.mark.parametrize("name", list(_EXEMPLARS))
def test_contract_is_immutable(name: str) -> None:
    """frozen=True: normal attribute assignment is rejected on every contract."""

    instance = _EXEMPLARS[name]
    with pytest.raises((ValidationError, TypeError)):
        instance.token_estimate = 999  # type: ignore[misc]


def test_checkpoint_rejects_inverted_source_range() -> None:
    """A checkpoint covers a non-negative, non-inverted event range (plan §10.2)."""

    with pytest.raises(ValidationError, match="source_end"):
        CompactionCheckpointV1.model_validate(
            {
                "checkpoint_id": "c",
                "source_start": 100,
                "source_end": 1,
                "source_digest": "x",
                "created_at": "2026-07-24T00:00:00Z",
            }
        )


def test_model_spec_rejects_non_positive_window() -> None:
    with pytest.raises(ValidationError):
        ModelContextSpec.model_validate(
            {"context_window_tokens": 0, "max_output_tokens": 1, "tokenizer": "x"}
        )


def test_zone_usage_rejects_share_above_one() -> None:
    with pytest.raises(ValidationError):
        ZoneUsage.model_validate({"zone": "system", "share": 1.5, "survival": "pinned"})


def test_checkpoint_item_rejects_confidence_above_one() -> None:
    with pytest.raises(ValidationError):
        CheckpointItem.model_validate({"id": "i", "text": "x", "confidence": 1.5})


def test_checkpoint_rejects_unknown_extra_field() -> None:
    """extra=forbid: an unrecognised field is rejected, so the schema is closed."""

    with pytest.raises(ValidationError):
        CompactionCheckpointV1.model_validate(
            {
                "checkpoint_id": "c",
                "source_start": 0,
                "source_end": 0,
                "source_digest": "x",
                "created_at": "2026-07-24T00:00:00Z",
                "schema_version": 1,
                "unexpected_field": True,
            }
        )


def test_external_content_keeps_distinct_trust_tag() -> None:
    """UNTRUSTED_EXTERNAL is a distinct value; external content can never read as
    SYSTEM at the type level. This is the type-level half of plan §19; the
    runtime half (refusing re-tagging) lands in M2."""

    block = ContextBlock(
        id="ext",
        zone=ContextZone.RETRIEVED_CONTEXT,
        source=ContextSource(kind=SourceKind.RETRIEVED, origin="mcp:web"),
        payload=ContextPayload(text="fetched doc"),
        token_estimate=5,
        priority=55,
        retention=RetentionPolicy.REDUCE,
        trust=TrustLevel.UNTRUSTED_EXTERNAL,
    )
    assert block.trust is TrustLevel.UNTRUSTED_EXTERNAL
    assert block.trust is not TrustLevel.SYSTEM
