"""Durable memory subsystem (M5).

SQLite is the authority; Markdown is an auditable projection. The manager
exposes recall/remember/forget; the engine's ``control`` wires ``/memory``
commands to it. Auto-learning (two-stage extraction/consolidation) is M6 and is
not wired here - by default ``memory.learn=false`` and no background model call
runs (plan §11.4, §11.6).
"""

from __future__ import annotations

from lumen.context.memory.manager import MemoryManager
from lumen.context.memory.projection import (
    MAX_INDEX_BYTES,
    MAX_INDEX_LINES,
    render_memory_index,
    render_topic,
)
from lumen.context.memory.records import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    Sensitivity,
)
from lumen.context.memory.repository import (
    InMemoryMemoryRepository,
    MemoryRepository,
    SQLiteMemoryRepository,
)

__all__ = [
    "MAX_INDEX_BYTES",
    "MAX_INDEX_LINES",
    "InMemoryMemoryRepository",
    "MemoryKind",
    "MemoryManager",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryScope",
    "MemorySource",
    "MemoryStatus",
    "SQLiteMemoryRepository",
    "Sensitivity",
    "render_memory_index",
    "render_topic",
]
