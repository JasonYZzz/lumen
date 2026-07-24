"""Tests for structured context assembly (M2): blocks, ordering, dedup, preflight."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from lumen.context.assembler import ContextAssembler, _BlockBuilder
from lumen.context.budget import ConservativeTokenCounter, DeterministicTokenCounter
from lumen.context.legacy import ContextBudgetExceeded
from lumen.context.types import ContextZone


def _assembler(window: int, counter=None, *, max_output: int = 4096) -> ContextAssembler:
    return ContextAssembler(
        window_tokens=window,
        max_output_tokens=max_output,
        counter=counter or ConservativeTokenCounter(),
    )


# --------------------------------------------------------------------------- #
# Blocks carry source/revision (acceptance: /context detail 可追溯来源)
# --------------------------------------------------------------------------- #


def test_assemble_emits_source_tracked_blocks_for_each_zone() -> None:
    """Every model-visible block has a zone, source origin and revision."""

    assembler = _assembler(200_000)
    assembled = assembler.assemble(
        instructions="You are Lumen.",
        prompt="do the thing",
        tool_schemas=[{"name": "read_file", "description": "read", "parameters": {}}],
        history=[ModelRequest(parts=[UserPromptPart(content="earlier turn")])],
    )
    zones = {block.zone for block in assembled.blocks}
    assert ContextZone.SYSTEM in zones
    assert ContextZone.CURRENT_INPUT in zones
    assert ContextZone.CAPABILITY_CATALOG in zones
    assert ContextZone.RECENT_HISTORY in zones
    assert ContextZone.OUTPUT_RESERVE in zones

    for block in assembled.blocks:
        assert block.source.origin  # provenance: traceable to an origin
        assert block.token_estimate >= 0
    # The system block carries a content revision (re-injection dedup key).
    system = next(b for b in assembled.blocks if b.zone is ContextZone.SYSTEM)
    assert system.source.revision is not None
    assert system.cache_key.startswith("runtime:instructions:")


def test_assemble_blocks_are_in_stable_zone_order() -> None:
    """Stable/pinned content first, then catalog, history, current input, reserve."""

    assembler = _assembler(200_000)
    assembled = assembler.assemble(
        instructions="system",
        prompt="prompt",
        tool_schemas=[{"name": "t", "description": "d", "parameters": {}}],
        history=[ModelRequest(parts=[UserPromptPart(content="h")])],
    )
    order = [block.zone for block in assembled.blocks]
    expected = [
        ContextZone.SYSTEM,
        ContextZone.CAPABILITY_CATALOG,
        ContextZone.RECENT_HISTORY,
        ContextZone.CURRENT_INPUT,
        ContextZone.OUTPUT_RESERVE,
    ]
    assert order == expected


def test_assemble_dedups_blocks_sharing_a_source_revision() -> None:
    """Two blocks with the same source revision are injected once (plan §6.2)."""

    counter = ConservativeTokenCounter()
    builder = _BlockBuilder(
        counter, __import__("lumen.context.assembler", fromlist=["ZoneCaps"]).ZoneCaps.for_window(200_000)
    )
    builder.add_system("same instructions")
    builder.add_system("same instructions")  # identical revision -> deduped
    blocks = builder.sorted_deduplicated()
    assert sum(1 for b in blocks if b.zone is ContextZone.SYSTEM) == 1


# --------------------------------------------------------------------------- #
# Fixed-context preflight (acceptance: 固定内容超限在调用 provider 前失败)
# --------------------------------------------------------------------------- #


def test_assemble_raises_when_fixed_content_exceeds_window() -> None:
    """Instructions + prompt + output reserve over the window fails before provider."""

    # per_text_char=1.0 -> 1 token per char; window=1000, reserve=50 (5%).
    counter = DeterministicTokenCounter(per_text_char=1.0)
    assembler = _assembler(1_000, counter, max_output=4_096)
    with pytest.raises(ContextBudgetExceeded, match="fixed context footprint exceeds"):
        assembler.assemble(
            instructions="i" * 800,  # 800 tokens
            prompt="p" * 200,  # 200 tokens -> 1000 fixed + 50 reserve > 1000
            tool_schemas=[],
            history=[],
        )


def test_assemble_passes_when_fixed_content_fits() -> None:
    counter = DeterministicTokenCounter(per_text_char=1.0)
    assembler = _assembler(2_000, counter)
    assembled = assembler.assemble(
        instructions="i" * 500,
        prompt="p" * 200,
        tool_schemas=[],
        history=[],
    )
    assert assembled.fixed_tokens == 700


# --------------------------------------------------------------------------- #
# Budget report (plan §14.1)
# --------------------------------------------------------------------------- #


def test_budget_report_groups_by_zone_and_sums_used_tokens() -> None:
    assembler = _assembler(200_000)
    assembled = assembler.assemble(
        instructions="You are Lumen. Be helpful.",
        prompt="read the file",
        tool_schemas=[{"name": "read_file", "description": "read", "parameters": {}}],
        history=[ModelRequest(parts=[UserPromptPart(content="earlier")])],
    )
    budget = assembled.budget
    assert budget.context_window_tokens == 200_000
    assert budget.used_tokens == sum(zone.tokens for zone in budget.zones)
    assert budget.output_reserve_tokens > 0
    # Every zone row has a survival policy.
    assert all(zone.survival for zone in budget.zones)


def test_budget_report_flags_over_cap_zone_as_pressure() -> None:
    """A zone exceeding its cap appears in the pressure list (plan §8.3)."""

    # per_tool=200 -> one tool's catalog is 200 tokens; window=1000 -> cap 8% = 80.
    counter = DeterministicTokenCounter(per_tool=200)
    assembler = _assembler(1_000, counter)
    assembled = assembler.assemble(
        instructions="sys",
        prompt="p",
        tool_schemas=[{"name": "t", "description": "d", "parameters": {}}],
        history=[],
    )
    labels = " ".join(item.label for item in assembled.budget.pressure)
    assert "capability_catalog over cap" in labels


def test_recent_history_uses_override_when_provided() -> None:
    """The engine passes a pre-computed history token count to avoid re-counting."""

    counter = ConservativeTokenCounter()
    assembler = _assembler(200_000, counter)
    history = [ModelRequest(parts=[UserPromptPart(content="x" * 400)])]
    assembled = assembler.assemble(
        instructions="sys",
        prompt="p",
        tool_schemas=[],
        history=history,
        history_token_override=123,
    )
    recent = next(b for b in assembled.blocks if b.zone is ContextZone.RECENT_HISTORY)
    assert recent.token_estimate == 123


def test_assemble_marks_estimated_window_in_budget() -> None:
    assembler = ContextAssembler(
        window_tokens=32_000,
        max_output_tokens=4_096,
        counter=ConservativeTokenCounter(),
        estimated_window=True,
    )
    assembled = assembler.assemble(instructions="sys", prompt="p", tool_schemas=[], history=[])
    assert assembled.budget.estimated is True
