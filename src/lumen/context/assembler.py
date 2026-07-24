"""Structured context assembly: blocks, ordering, caps and preflight (M2).

Turns a request's stable prefix (system instructions, tool-schema catalog) plus
its dynamic content (current prompt, recent history) into a list of
source-tracked :class:`ContextBlock` values and a real
:class:`ContextBudgetReport`, so ``/context`` can explain every model-visible
byte and a request whose fixed footprint cannot fit fails before the provider
call (plan §6, §8.2, §8.3, §14.1).

M2 assembles the zones the current runtime actually produces: SYSTEM
(instructions), CURRENT_INPUT (prompt), CAPABILITY_CATALOG (tool schemas),
RECENT_HISTORY and OUTPUT_RESERVE. MEMORY_INDEX / TASK_STATE /
HISTORY_SUMMARY / RECALLED_MEMORY / RETRIEVED_CONTEXT stay empty until M3-M6;
they are present in the report as zero-token rows so ``/context`` already shows
the full zone map.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelMessage

from lumen.context.budget import (
    TokenCounter,
    raise_if_fixed_context_exceeds_window,
)
from lumen.context.types import (
    ContextBlock,
    ContextBudgetReport,
    ContextPayload,
    ContextSource,
    ContextZone,
    EvidenceRef,
    PressureItem,
    RetentionPolicy,
    SourceKind,
    TrustLevel,
    ZoneUsage,
)

#: Zone caps as a fraction of the model window (plan §8.3). Pinned zones
#: (SYSTEM, CURRENT_INPUT, OUTPUT_RESERVE) have no cap - they may not be
#: dropped; if they exceed the window the preflight raises instead.
ZONE_CAP_RATIOS: dict[ContextZone, float | None] = {
    ContextZone.SYSTEM: None,
    ContextZone.POLICY: None,
    ContextZone.CURRENT_INPUT: None,
    ContextZone.OUTPUT_RESERVE: None,
    ContextZone.MEMORY_INDEX: 0.04,
    ContextZone.RECALLED_MEMORY: 0.06,
    ContextZone.TASK_STATE: 0.10,
    ContextZone.HISTORY_SUMMARY: 0.05,
    ContextZone.CAPABILITY_CATALOG: 0.08,
    ContextZone.RECENT_HISTORY: None,  # token-budgeted, not ratio-capped
    ContextZone.RETRIEVED_CONTEXT: 0.20,
}

#: Stable assembly order and survival policy per zone (plan §6.1, §8.2).
#: Pinned/stable content first, then session state, then the compressible tail.
_ZONE_ORDER: tuple[tuple[ContextZone, RetentionPolicy, TrustLevel], ...] = (
    (ContextZone.SYSTEM, RetentionPolicy.PINNED, TrustLevel.SYSTEM),
    (ContextZone.POLICY, RetentionPolicy.REINJECT, TrustLevel.POLICY),
    (ContextZone.MEMORY_INDEX, RetentionPolicy.REINJECT, TrustLevel.DURABLE),
    (ContextZone.CAPABILITY_CATALOG, RetentionPolicy.REINJECT, TrustLevel.SYSTEM),
    (ContextZone.TASK_STATE, RetentionPolicy.REINJECT, TrustLevel.SYSTEM),
    (ContextZone.HISTORY_SUMMARY, RetentionPolicy.SUMMARIZE, TrustLevel.RECALLED),
    (ContextZone.RECENT_HISTORY, RetentionPolicy.PINNED, TrustLevel.RECALLED),
    (ContextZone.RECALLED_MEMORY, RetentionPolicy.EPHEMERAL, TrustLevel.RECALLED),
    (ContextZone.RETRIEVED_CONTEXT, RetentionPolicy.REDUCE, TrustLevel.UNTRUSTED_EXTERNAL),
    (ContextZone.CURRENT_INPUT, RetentionPolicy.PINNED, TrustLevel.SYSTEM),
    (ContextZone.OUTPUT_RESERVE, RetentionPolicy.RESERVE_ONLY, TrustLevel.SYSTEM),
)

_ZONE_INDEX = {zone: i for i, (zone, _, _) in enumerate(_ZONE_ORDER)}


@dataclass(frozen=True, slots=True)
class ZoneCaps:
    """Resolved per-zone token caps (plan §8.3), derived from the window."""

    caps: dict[ContextZone, int]

    @classmethod
    def for_window(cls, window_tokens: int) -> ZoneCaps:
        caps: dict[ContextZone, int] = {}
        for zone, ratio in ZONE_CAP_RATIOS.items():
            caps[zone] = int(window_tokens * ratio) if ratio is not None else window_tokens
        return cls(caps)


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """The assembler's output: ordered blocks plus the budget report."""

    blocks: tuple[ContextBlock, ...]
    budget: ContextBudgetReport
    #: Tokens for the pinned fixed prefix (SYSTEM + POLICY + CURRENT_INPUT),
    #: used by the preflight invariant (must fit alongside the output reserve).
    fixed_tokens: int
    output_reserve_tokens: int


@dataclass
class ContextAssembler:
    """Build source-tracked blocks and a budget report for one request.

    The assembler is stateless across requests; the engine holds one instance
    configured with the resolved model spec and token counter.
    """

    window_tokens: int
    max_output_tokens: int
    counter: TokenCounter
    estimated_window: bool = False
    session_id: str | None = None

    # -- public -----------------------------------------------------------

    def assemble(
        self,
        *,
        instructions: str,
        prompt: str,
        tool_schemas: Sequence[dict[str, Any]],
        history: Sequence[ModelMessage],
        history_token_override: int | None = None,
    ) -> AssembledContext:
        """Assemble blocks, enforce caps + preflight, and report the budget."""

        output_reserve = min(self.max_output_tokens, int(self.window_tokens * 0.05))
        output_reserve = max(output_reserve, 1)
        builder = _BlockBuilder(self.counter, ZoneCaps.for_window(self.window_tokens))
        builder.add_system(instructions)
        builder.add_current_input(prompt)
        builder.add_capability_catalog(tool_schemas)
        builder.add_recent_history(history, history_token_override)
        builder.add_output_reserve(output_reserve)

        blocks = builder.sorted_deduplicated()
        fixed_tokens = builder.fixed_tokens
        # Fixed-content preflight: the stable prefix + current input must fit
        # alongside the output reserve before any history is sent (plan §8.2 #3).
        raise_if_fixed_context_exceeds_window(
            fixed_tokens=fixed_tokens,
            window_tokens=self.window_tokens,
            output_reserve=output_reserve,
        )
        budget = self._budget_report(blocks, output_reserve, fixed_tokens)
        return AssembledContext(
            blocks=blocks,
            budget=budget,
            fixed_tokens=fixed_tokens,
            output_reserve_tokens=output_reserve,
        )

    # -- internal ---------------------------------------------------------

    def _budget_report(
        self,
        blocks: tuple[ContextBlock, ...],
        output_reserve: int,
        fixed_tokens: int,
    ) -> ContextBudgetReport:
        """Group blocks by zone into the ``/context`` table (plan §14.1)."""

        by_zone: dict[ContextZone, int] = {}
        for block in blocks:
            by_zone[block.zone] = by_zone.get(block.zone, 0) + block.token_estimate
        zones: list[ZoneUsage] = []
        pressure: list[PressureItem] = []
        used = sum(by_zone.values())
        for zone, retention, _trust in _ZONE_ORDER:
            tokens = by_zone.get(zone, 0)
            if tokens == 0 and zone not in by_zone:
                continue
            share = round(tokens / self.window_tokens, 4) if self.window_tokens else 0.0
            zones.append(ZoneUsage(zone=zone, tokens=tokens, share=share, survival=retention))
            cap = ZONE_CAP_RATIOS.get(zone)
            if cap is not None and tokens > int(self.window_tokens * cap):
                pressure.append(
                    PressureItem(
                        label=f"{zone.value} over cap",
                        tokens=tokens,
                        source=f"cap={int(self.window_tokens * cap)}",
                    )
                )
        # Surface the largest blocks as pressure regardless of caps.
        for block in sorted(blocks, key=lambda b: b.token_estimate, reverse=True)[:3]:
            if block.token_estimate > 0:
                pressure.append(
                    PressureItem(
                        label=f"{block.zone.value}:{block.source.origin}",
                        tokens=block.token_estimate,
                        source=block.id,
                    )
                )
        return ContextBudgetReport(
            context_window_tokens=self.window_tokens,
            used_tokens=used,
            output_reserve_tokens=output_reserve,
            soft_threshold_tokens=int(self.window_tokens * 0.80),
            hard_threshold_tokens=int(self.window_tokens * 0.92),
            target_tokens=int(self.window_tokens * 0.55),
            estimated=self.estimated_window,
            zones=tuple(zones),
            pressure=tuple(pressure),
        )


class _BlockBuilder:
    """Accumulates blocks, dedups by source revision, sums the fixed prefix."""

    def __init__(self, counter: TokenCounter, caps: ZoneCaps) -> None:
        self.counter = counter
        self.caps = caps
        self.blocks: list[ContextBlock] = []
        self.seen_sources: set[tuple[str, str, str | None]] = set()

    @property
    def fixed_tokens(self) -> int:
        """Tokens of the pinned fixed prefix (SYSTEM + POLICY + CURRENT_INPUT)."""

        return sum(
            b.token_estimate
            for b in self.blocks
            if b.zone in {ContextZone.SYSTEM, ContextZone.POLICY, ContextZone.CURRENT_INPUT}
        )

    def add(self, block: ContextBlock) -> None:
        """Append ``block``, skipping a duplicate source revision (plan §6.2)."""

        key = (block.source.kind.value, block.source.origin, block.source.revision)
        if key in self.seen_sources:
            return
        self.seen_sources.add(key)
        self.blocks.append(block)

    def add_system(self, instructions: str) -> None:
        if not instructions:
            return
        self.add(
            self._block(
                "system-instructions",
                ContextZone.SYSTEM,
                SourceKind.SYSTEM,
                "runtime:instructions",
                self._revision(instructions),
                ContextPayload(text=instructions),
                self.counter.count_text(instructions).tokens,
                100,
                RetentionPolicy.PINNED,
                TrustLevel.SYSTEM,
            )
        )

    def add_current_input(self, prompt: str) -> None:
        if not prompt:
            return
        self.add(
            self._block(
                "current-input",
                ContextZone.CURRENT_INPUT,
                SourceKind.CURRENT,
                "user:prompt",
                None,  # per-turn, never deduped against a prior revision
                ContextPayload(text=prompt),
                self.counter.count_text(prompt).tokens,
                100,
                RetentionPolicy.PINNED,
                TrustLevel.SYSTEM,
            )
        )

    def add_capability_catalog(self, schemas: Sequence[dict[str, Any]]) -> None:
        if not schemas:
            return
        payload = ContextPayload(structured={"tools": list(schemas)})
        self.add(
            self._block(
                "capability-catalog",
                ContextZone.CAPABILITY_CATALOG,
                SourceKind.CAPABILITY,
                "runtime:tool-schemas",
                self._revision(str(sorted(s["name"] for s in schemas))),
                payload,
                self.counter.count_tools(schemas).tokens,
                85,
                RetentionPolicy.REINJECT,
                TrustLevel.SYSTEM,
            )
        )

    def add_recent_history(self, history: Sequence[ModelMessage], override: int | None) -> None:
        if not history:
            return
        tokens = override if override is not None else self.counter.count_messages(history).tokens
        digest = hashlib.sha256(
            repr(
                [
                    (type(m).__name__, [str(getattr(p, "content", "")) for p in getattr(m, "parts", [])])
                    for m in history
                ]
            ).encode("utf-8")
        ).hexdigest()
        self.add(
            self._block(
                "recent-history",
                ContextZone.RECENT_HISTORY,
                SourceKind.HISTORY,
                "session:recent-history",
                digest,
                ContextPayload(structured={"message_count": len(list(history))}),
                tokens,
                75,
                RetentionPolicy.PINNED,
                TrustLevel.RECALLED,
                provenance=(EvidenceRef(session_id=None, detail="recent window"),),
            )
        )

    def add_output_reserve(self, output_reserve: int) -> None:
        self.add(
            self._block(
                "output-reserve",
                ContextZone.OUTPUT_RESERVE,
                SourceKind.RESERVE,
                "runtime:output-reserve",
                None,
                ContextPayload(text=None),
                output_reserve,
                100,
                RetentionPolicy.RESERVE_ONLY,
                TrustLevel.SYSTEM,
            )
        )

    def sorted_deduplicated(self) -> tuple[ContextBlock, ...]:
        """Return blocks in stable zone order (plan §6.2 ordering invariant)."""

        return tuple(sorted(self.blocks, key=lambda b: _ZONE_INDEX[b.zone]))

    def _block(
        self,
        block_id: str,
        zone: ContextZone,
        kind: SourceKind,
        origin: str,
        revision: str | None,
        payload: ContextPayload,
        tokens: int,
        priority: int,
        retention: RetentionPolicy,
        trust: TrustLevel,
        provenance: tuple[EvidenceRef, ...] = (),
    ) -> ContextBlock:
        return ContextBlock(
            id=block_id,
            zone=zone,
            source=ContextSource(kind=kind, origin=origin, revision=revision),
            payload=payload,
            token_estimate=tokens,
            priority=priority,
            retention=retention,
            trust=trust,
            cache_key=f"{origin}:{revision}" if revision else origin,
            provenance=provenance,
        )

    @staticmethod
    def _revision(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ZONE_CAP_RATIOS",
    "AssembledContext",
    "ContextAssembler",
    "ZoneCaps",
]
