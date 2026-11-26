"""Markdown projection of durable memory (M5).

Renders the SQLite authority into a human-auditable MEMORY.md + per-topic views
(plan §11.2). SQLite is the single source of truth; Markdown is a projection
rebuilt atomically from it, so ``/memory edit`` exports to Markdown, validates,
and writes back via the repository - the file is never a second master.

Restricted-sensitivity records are never projected (plan §11.6). Markdown that
could be re-parsed as a config include is escaped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from lumen.context.memory.records import MemoryRecord, MemoryScope, MemoryStatus, Sensitivity

#: Hard caps on the projected index (plan §11.2: "最多 200 行/25 KiB").
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_600


def render_memory_index(records: Iterable[MemoryRecord]) -> str:
    """Render the MEMORY.md index from active, non-restricted records.

    The index is one bullet per record, grouped by scope, capped to
    ``MAX_INDEX_LINES`` lines and ``MAX_INDEX_BYTES`` bytes so it never crowds
    the context window when re-injected.
    """

    return select_memory_index(records)[0]


def select_memory_index(
    records: Iterable[MemoryRecord], fits: Callable[[str], bool] | None = None,
) -> tuple[str, set[str]]:
    """Select complete records once, including section overhead in the provider budget."""

    text = "# 记忆索引\n"
    included: set[str] = set()
    by_scope: dict[MemoryScope, list[MemoryRecord]] = {
        MemoryScope.USER: [],
        MemoryScope.PROJECT: [],
        MemoryScope.PATH: [],
        MemoryScope.AGENT: [],
    }
    for record in records:
        if record.status is not MemoryStatus.ACTIVE:
            continue
        if record.sensitivity is Sensitivity.RESTRICTED:
            continue
        by_scope.setdefault(record.scope, []).append(record)

    for scope in (MemoryScope.USER, MemoryScope.PROJECT, MemoryScope.PATH, MemoryScope.AGENT):
        header = f"\n## {scope.value}\n"
        for record in by_scope[scope]:
            candidate = f"{text}{header}- [{record.id}] {record.content}\n"
            if len(candidate.encode("utf-8")) > MAX_INDEX_BYTES or candidate.count("\n") > MAX_INDEX_LINES:
                continue
            if fits is not None and not fits(candidate):
                continue
            text = candidate
            included.add(record.id)
            header = ""
    return (text if included or fits is None else ""), included


def render_topic(records: Iterable[MemoryRecord], *, title: str) -> str:
    """Render one topic view with full provenance for audit (plan §11.2)."""

    lines = [f"# {title}", ""]
    for record in records:
        if record.sensitivity is Sensitivity.RESTRICTED:
            continue
        lines.append(f"## {record.id}")
        lines.append(f"- scope: {record.scope.value}")
        lines.append(f"- kind: {record.kind.value}")
        lines.append(f"- status: {record.status.value}")
        lines.append(f"- confidence: {record.confidence}")
        lines.append(f"- content: {record.content}")
        if record.supersedes:
            lines.append(f"- supersedes: {', '.join(record.supersedes)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "MAX_INDEX_BYTES",
    "MAX_INDEX_LINES",
    "render_memory_index",
    "render_topic",
]
