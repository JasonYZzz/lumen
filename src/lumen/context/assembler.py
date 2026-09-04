"""Budget and retention assembly for model-visible context.

Turns a request's stable prefix (system instructions, tool-schema catalog) plus
its dynamic content (current prompt, recent history) into a list of
source-tracked :class:`ContextBlock` values and a real
:class:`ContextBudgetReport`, so ``/context`` can explain every model-visible
byte and a request whose fixed footprint cannot fit fails before the provider
call (plan §6, §8.2, §8.3, §14.1).

Zones explain cost, provenance, trust, and retention. They do not define the
provider's role/message payload; :mod:`lumen.context.engine` renders those
blocks into native message parts after budgeting.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    NativeToolSearchReturnPart,
    ToolSearchReturnPart,
)

from lumen.context.budget import (
    TokenCounter,
    raise_if_fixed_context_exceeds_window,
)
from lumen.context.legacy import ContextBudgetExceeded
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
    ContextZone.ACTIVE_SKILLS: 0.10,
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
    (ContextZone.ACTIVE_SKILLS, RetentionPolicy.REINJECT, TrustLevel.POLICY),
    (ContextZone.HISTORY_SUMMARY, RetentionPolicy.SUMMARIZE, TrustLevel.RECALLED),
    (ContextZone.RECENT_HISTORY, RetentionPolicy.PINNED, TrustLevel.RECALLED),
    (ContextZone.RECALLED_MEMORY, RetentionPolicy.EPHEMERAL, TrustLevel.RECALLED),
    (ContextZone.RETRIEVED_CONTEXT, RetentionPolicy.REDUCE, TrustLevel.UNTRUSTED_EXTERNAL),
    (ContextZone.CURRENT_INPUT, RetentionPolicy.PINNED, TrustLevel.USER),
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
    output_reserve_tokens: int | None = None
    estimated_window: bool = False
    soft_limit_tokens: int | None = None
    hard_limit_tokens: int | None = None
    target_tokens: int | None = None

    # -- public -----------------------------------------------------------

    def assemble(
        self,
        *,
        instructions: str,
        policy: str = "",
        prompt: str,
        tool_schemas: Sequence[dict[str, Any]],
        history: Sequence[ModelMessage],
        memory_index: str = "",
        recalled_memory: str = "",
        active_skills: Sequence[dict[str, Any]] = (),
        retrieved_context: Sequence[dict[str, Any]] = (),
        task_state: str = "",
        history_token_override: int | None = None,
    ) -> AssembledContext:
        """Assemble blocks, enforce caps + preflight, and report the budget."""

        # Policy-aware engines provide the complete requested output reserve.
        # The legacy cap remains only for direct construction compatibility,
        # where ``max_output_tokens`` historically meant an architectural max.
        output_reserve = (
            max(self.output_reserve_tokens, 1)
            if self.output_reserve_tokens is not None
            else max(min(self.max_output_tokens, int(self.window_tokens * 0.05)), 1)
        )
        builder = _BlockBuilder(self.counter, ZoneCaps.for_window(self.window_tokens))
        builder.add_system(instructions)
        builder.add_policy(policy)
        builder.add_current_input(prompt)
        builder.add_memory(memory_index, recalled_memory)
        contextual_schemas = _contextual_tool_documents(tool_schemas, history)
        builder.add_capability_catalog(contextual_schemas)
        builder.add_active_skills(active_skills)
        builder.add_task_state(task_state)
        builder.add_retrieved_context(retrieved_context)
        builder.add_history(history, history_token_override)
        builder.add_output_reserve(output_reserve)

        blocks = builder.sorted_blocks()
        fixed_tokens = builder.fixed_tokens
        # Fixed-content preflight: the stable prefix + current input must fit
        # alongside the output reserve before any history is sent (plan §8.2 #3).
        raise_if_fixed_context_exceeds_window(
            fixed_tokens=fixed_tokens,
            window_tokens=self.window_tokens,
            output_reserve=output_reserve,
        )
        budget = self._budget_report(blocks, output_reserve)
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
            soft_threshold_tokens=self.soft_limit_tokens or int(self.window_tokens * 0.80),
            hard_threshold_tokens=self.hard_limit_tokens or int(self.window_tokens * 0.92),
            target_tokens=self.target_tokens or int(self.window_tokens * 0.55),
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
        """Tokens always sent before reducible history."""

        return sum(
            b.token_estimate
            for b in self.blocks
            if b.zone
            in {
                ContextZone.SYSTEM,
                ContextZone.POLICY,
                ContextZone.MEMORY_INDEX,
                ContextZone.RECALLED_MEMORY,
                ContextZone.CAPABILITY_CATALOG,
                ContextZone.TASK_STATE,
                ContextZone.ACTIVE_SKILLS,
                ContextZone.RETRIEVED_CONTEXT,
                ContextZone.CURRENT_INPUT,
            }
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

    def add_policy(self, policy: str) -> None:
        if not policy:
            return
        self.add(
            self._block(
                "policy-instructions",
                ContextZone.POLICY,
                SourceKind.POLICY,
                "runtime:policy",
                self._revision(policy),
                ContextPayload(text=policy),
                self.counter.count_text(policy).tokens,
                100,
                RetentionPolicy.REINJECT,
                TrustLevel.POLICY,
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
                TrustLevel.USER,
            )
        )

    def add_capability_catalog(self, schemas: Sequence[dict[str, Any]]) -> None:
        if not schemas:
            return
        cap = self.caps.caps[ContextZone.CAPABILITY_CATALOG]
        loaded = [schema for schema in schemas if schema.get("loaded") is not False]
        loaded_tokens = self.counter.count_tools(loaded).tokens
        if loaded_tokens > cap:
            raise ContextBudgetExceeded(
                "capability_catalog zone exceeds its enforced cap: "
                f"{loaded_tokens} > {cap} tokens; defer schemas or reduce always_load_tools"
            )
        selected = list(loaded)
        for schema in schemas:
            if schema.get("loaded") is not False:
                continue
            candidate = [*selected, schema]
            if self.counter.count_tools(candidate).tokens <= cap:
                selected.append(schema)
        # Preserve the source order after prioritising every provider-visible
        # loaded schema over optional deferred catalog entries.
        selected_ids = {id(schema) for schema in selected}
        selected = [schema for schema in schemas if id(schema) in selected_ids]
        payload = ContextPayload(structured={"tools": selected, "omitted": len(schemas) - len(selected)})
        self.add(
            self._block(
                "capability-catalog",
                ContextZone.CAPABILITY_CATALOG,
                SourceKind.CAPABILITY,
                "runtime:tool-schemas",
                self._revision(str(sorted(s["name"] for s in selected))),
                payload,
                self.counter.count_tools(selected).tokens,
                85,
                RetentionPolicy.REINJECT,
                TrustLevel.SYSTEM,
            )
        )

    def add_memory(self, index: str, recalled: str) -> None:
        if index:
            index = self._fit_text(index, self.caps.caps[ContextZone.MEMORY_INDEX])
            self.add(
                self._block(
                    "memory-index",
                    ContextZone.MEMORY_INDEX,
                    SourceKind.MEMORY,
                    "memory:index",
                    self._revision(index),
                    ContextPayload(text=index),
                    self.counter.count_text(index).tokens,
                    90,
                    RetentionPolicy.REINJECT,
                    TrustLevel.DURABLE,
                )
            )
        if recalled:
            recalled = self._fit_text(recalled, self.caps.caps[ContextZone.RECALLED_MEMORY])
            self.add(
                self._block(
                    "recalled-memory",
                    ContextZone.RECALLED_MEMORY,
                    SourceKind.MEMORY,
                    "memory:recall",
                    self._revision(recalled),
                    ContextPayload(text=recalled),
                    self.counter.count_text(recalled).tokens,
                    65,
                    RetentionPolicy.EPHEMERAL,
                    TrustLevel.RECALLED,
                )
            )

    def _fit_text(self, text: str, cap: int) -> str:
        if self.counter.count_text(text).tokens <= cap:
            return text
        marker = "\n[truncated to zone cap]"
        if self.counter.count_text(marker).tokens > cap:
            marker = ""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = text[:middle] + marker
            if self.counter.count_text(candidate).tokens <= cap:
                low = middle
            else:
                high = middle - 1
        return text[:low] + marker

    def add_active_skills(self, skills: Sequence[dict[str, Any]]) -> None:
        remaining = self.caps.caps[ContextZone.ACTIVE_SKILLS]
        selected: list[tuple[dict[str, Any], str]] = []
        # ResourceManager exposes the working set in LRU order. Fill the zone
        # newest-first, then restore stable source order for rendering.
        for skill in reversed(skills):
            body = str(skill.get("body", ""))
            if not body or remaining <= 0:
                continue
            fitted = self._fit_text(body, remaining)
            tokens = self.counter.count_text(fitted).tokens
            if not fitted or tokens <= 0:
                continue
            selected.append((skill, fitted))
            remaining -= tokens
        for skill, body in reversed(selected):
            name = str(skill.get("name", "unknown"))
            self.add(
                self._block(
                    f"active-skill:{name}",
                    ContextZone.ACTIVE_SKILLS,
                    SourceKind.CAPABILITY,
                    f"skill:{name}",
                    str(skill.get("revision") or self._revision(body)),
                    ContextPayload(
                        text=body,
                        structured={
                            "name": name,
                            "revision": str(skill.get("revision") or self._revision(body)),
                            "source": str(skill.get("source") or skill.get("path") or f"skill:{name}"),
                            "body_artifact_ref": skill.get("body_artifact_ref"),
                        },
                    ),
                    self.counter.count_text(body).tokens,
                    92,
                    RetentionPolicy.REINJECT,
                    TrustLevel.POLICY,
                )
            )

    def add_task_state(self, task_state: str) -> None:
        if not task_state:
            return
        fitted = self._fit_text(task_state, self.caps.caps[ContextZone.TASK_STATE])
        self.add(
            self._block(
                "task-state",
                ContextZone.TASK_STATE,
                SourceKind.TASK,
                "session:task-state",
                self._revision(fitted),
                ContextPayload(text=fitted),
                self.counter.count_text(fitted).tokens,
                95,
                RetentionPolicy.REINJECT,
                TrustLevel.SYSTEM,
            )
        )

    def add_retrieved_context(self, documents: Sequence[dict[str, Any]]) -> None:
        remaining = self.caps.caps[ContextZone.RETRIEVED_CONTEXT]
        for document in documents:
            if remaining <= 0:
                break
            body = str(document.get("body", ""))
            if not body:
                continue
            fitted = self._fit_text(body, remaining)
            tokens = self.counter.count_text(fitted).tokens
            if not fitted or tokens <= 0:
                continue
            server = str(document.get("server", "unknown"))
            uri = str(document.get("uri", document.get("name", "resource")))
            self.add(
                self._block(
                    f"mcp-resource:{server}:{self._revision(uri)}",
                    ContextZone.RETRIEVED_CONTEXT,
                    SourceKind.RETRIEVED,
                    f"mcp:{server}:{uri}",
                    str(document.get("revision") or self._revision(body)),
                    ContextPayload(
                        text=fitted,
                        structured={
                            "server": server,
                            "uri": uri,
                            "revision": str(document.get("revision") or self._revision(body)),
                            "body_artifact_ref": document.get("body_artifact_ref"),
                        },
                    ),
                    tokens,
                    70,
                    RetentionPolicy.REDUCE,
                    TrustLevel.UNTRUSTED_EXTERNAL,
                )
            )
            remaining -= tokens

    def add_history(self, history: Sequence[ModelMessage], override: int | None) -> None:
        if not history:
            return
        summaries = [message for message in history if _is_history_summary(message)]
        recent = [message for message in history if not _is_history_summary(message)]
        if summaries:
            summary_tokens = self.counter.count_messages(summaries).tokens
            self.add(
                self._block(
                    "history-summary",
                    ContextZone.HISTORY_SUMMARY,
                    SourceKind.HISTORY,
                    "session:history-summary",
                    _messages_digest(summaries),
                    ContextPayload(structured={"message_count": len(summaries)}),
                    summary_tokens,
                    88,
                    RetentionPolicy.SUMMARIZE,
                    TrustLevel.RECALLED,
                    provenance=(EvidenceRef(session_id=None, detail="compaction summary"),),
                )
            )
        if not recent:
            return
        tokens = (
            override if override is not None and not summaries else self.counter.count_messages(recent).tokens
        )
        digest = _messages_digest(recent)
        self.add(
            self._block(
                "recent-history",
                ContextZone.RECENT_HISTORY,
                SourceKind.HISTORY,
                "session:recent-history",
                digest,
                ContextPayload(structured={"message_count": len(recent)}),
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

    def sorted_blocks(self) -> tuple[ContextBlock, ...]:
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


def _contextual_tool_documents(
    schemas: Sequence[dict[str, Any]],
    history: Sequence[ModelMessage],
) -> list[dict[str, Any]]:
    """Keep only names/descriptions until deferred tools are discovered."""

    discovered: set[str] = set()
    for message in history:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolSearchReturnPart):
                    discovered.update(item["name"] for item in part.content["discovered_tools"])
        else:
            for part in message.parts:
                if isinstance(part, NativeToolSearchReturnPart):
                    discovered.update(item["name"] for item in part.content["discovered_tools"])
    result: list[dict[str, Any]] = []
    for schema in schemas:
        if schema.get("deferred") is True and schema.get("name") not in discovered:
            result.append(
                {
                    "name": schema.get("name"),
                    "description": schema.get("description", ""),
                    "deferred": True,
                    "loaded": False,
                    "origin": schema.get("origin"),
                }
            )
        else:
            visible = dict(schema)
            visible["loaded"] = True
            result.append(visible)
    return result


def _messages_digest(messages: Sequence[ModelMessage]) -> str:
    return hashlib.sha256(
        repr(
            [
                (
                    type(message).__name__,
                    [str(getattr(part, "content", "")) for part in getattr(message, "parts", [])],
                )
                for message in messages
            ]
        ).encode("utf-8")
    ).hexdigest()


def _is_history_summary(message: ModelMessage) -> bool:
    """Recognise v1-v4 summaries and new explicitly tagged summaries."""

    raw_metadata = getattr(message, "metadata", None)
    metadata: dict[str, Any] = cast(dict[str, Any], raw_metadata) if isinstance(raw_metadata, dict) else {}
    if metadata.get("lumen_context_zone") == ContextZone.HISTORY_SUMMARY.value:
        return True
    if not isinstance(message, ModelRequest):
        return False
    for part in message.parts:
        content = str(getattr(part, "content", ""))
        if content.startswith("Prior conversation summary:") or content.startswith(
            '<history-summary version="1"'
        ):
            return True
    return False


__all__ = [
    "ZONE_CAP_RATIOS",
    "AssembledContext",
    "ContextAssembler",
    "ZoneCaps",
]
