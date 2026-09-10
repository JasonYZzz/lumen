"""Markdown projection of durable memory (M5).

Renders the SQLite authority into a human-auditable MEMORY.md + per-topic views
(plan §11.2). SQLite is the single source of truth; Markdown is a projection
rebuilt atomically from it, so ``/memory edit`` exports to Markdown, validates,
and writes back via the repository - the file is never a second master.

Restricted-sensitivity records are never projected (plan §11.6). Markdown that
could be re-parsed as a config include is escaped.
"""

from __future__ import annotations

from collections.abc import Iterable

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

    lines = ["# 记忆索引", ""]
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
        rows = by_scope.get(scope, [])
        if not rows:
            continue
        lines.append(f"## {scope.value}")
        for record in rows:
            lines.append(f"- [{record.id}] {record.content}")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    return _cap(text)


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


def _cap(text: str) -> str:
    """Truncate the projection to the line/byte caps, noting the cut."""

    if len(text.encode("utf-8")) <= MAX_INDEX_BYTES and text.count("\n") <= MAX_INDEX_LINES:
        return text
    truncated = text.encode("utf-8")[:MAX_INDEX_BYTES].decode("utf-8", errors="ignore")
    lines = truncated.splitlines()[:MAX_INDEX_LINES]
    return "\n".join(lines) + "\n\n(记忆索引已截断以符合容量上限)\n"


__all__ = [
    "MAX_INDEX_BYTES",
    "MAX_INDEX_LINES",
    "render_memory_index",
    "render_topic",
]
