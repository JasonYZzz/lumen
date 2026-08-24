"""Tests for structured context assembly (M2): blocks, ordering, dedup, preflight."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelRequest, ToolSearchReturnPart, UserPromptPart

from lumen.context.assembler import ContextAssembler
from lumen.context.budget import ConservativeTokenCounter, DeterministicTokenCounter, TokenCounter
from lumen.context.legacy import ContextBudgetExceeded
from lumen.context.types import ContextZone


def _assembler(
    window: int, counter: TokenCounter | None = None, *, max_output: int = 4096
) -> ContextAssembler:
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
    assert system.cache_key is not None
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


def test_assemble_rejects_visible_tool_schemas_above_catalog_cap() -> None:
    counter = DeterministicTokenCounter(per_text_char=0.0, per_tool=1_000)
    assembler = _assembler(1_000, counter, max_output=100)

    with pytest.raises(ContextBudgetExceeded, match="capability_catalog zone exceeds"):
        assembler.assemble(
            instructions="sys",
            prompt="prompt",
            tool_schemas=[{"name": "huge", "description": "large", "parameters": {}}],
            history=[],
        )


def test_deferred_schema_uses_catalog_cost_until_tool_search_discovers_it() -> None:
    counter = ConservativeTokenCounter()
    assembler = _assembler(1_000, counter, max_output=100)
    schema = {
        "name": "remote_huge",
        "description": "Search remote records",
        "parameters": {
            "type": "object",
            "description": "x" * 20_000,
            "properties": {"query": {"type": "string"}},
        },
        "deferred": True,
        "origin": "mcp:remote",
    }

    undiscovered = assembler.assemble(
        instructions="sys",
        prompt="prompt",
        tool_schemas=[schema],
        history=[],
    )
    catalog = next(block for block in undiscovered.blocks if block.zone is ContextZone.CAPABILITY_CATALOG)
    assert catalog.payload.structured is not None
    assert catalog.payload.structured["tools"][0].get("parameters") is None

    discovered_history = [
        ModelRequest(
            parts=[
                ToolSearchReturnPart(
                    content={"discovered_tools": [{"name": "remote_huge"}]},
                    tool_call_id="search-1",
                )
            ]
        )
    ]
    with pytest.raises(ContextBudgetExceeded):
        assembler.assemble(
            instructions="sys",
            prompt="prompt",
            tool_schemas=[schema],
            history=discovered_history,
        )


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
    """An indivisible loaded schema over its cap fails before the provider call."""

    # per_tool=200 -> one tool's catalog is 200 tokens; window=1000 -> cap 8% = 80.
    counter = DeterministicTokenCounter(per_tool=200)
    assembler = _assembler(1_000, counter)
    with pytest.raises(ContextBudgetExceeded, match="capability_catalog zone exceeds"):
        assembler.assemble(
            instructions="sys",
            prompt="p",
            tool_schemas=[{"name": "t", "description": "d", "parameters": {}}],
            history=[],
        )


def test_memory_zones_are_trimmed_to_their_enforced_caps() -> None:
    counter = DeterministicTokenCounter(per_text_char=1.0)
    assembler = _assembler(1_000, counter, max_output=10)

    assembled = assembler.assemble(
        instructions="sys",
        prompt="p",
        tool_schemas=[],
        history=[],
        memory_index="M" * 200,
        recalled_memory="R" * 200,
    )

    memory = next(block for block in assembled.blocks if block.zone is ContextZone.MEMORY_INDEX)
    recalled = next(block for block in assembled.blocks if block.zone is ContextZone.RECALLED_MEMORY)
    assert memory.token_estimate <= 40
    assert recalled.token_estimate <= 60
    assert memory.payload.text is not None and len(memory.payload.text) < 200
    assert recalled.payload.text is not None and len(recalled.payload.text) < 200


def test_active_skill_working_set_keeps_most_recent_bodies_within_task_cap() -> None:
    counter = DeterministicTokenCounter(per_text_char=1.0)
    assembler = _assembler(1_000, counter, max_output=10)

    assembled = assembler.assemble(
        instructions="sys",
        prompt="p",
        tool_schemas=[],
        history=[],
        active_skills=(
            {"name": "old", "body": "O" * 80, "revision": "old-r1"},
            {"name": "new", "body": "N" * 80, "revision": "new-r1"},
        ),
    )

    skills = [block for block in assembled.blocks if block.source.origin.startswith("skill:")]
    assert sum(block.token_estimate for block in skills) <= 100
    assert any(block.source.origin == "skill:new" for block in skills)


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
