"""Durable explicit memory and opt-in two-stage automatic learning.

SQLite is the authority and Markdown is an auditable projection. Automatic
learning uses a durable outbox, host-owned provenance, sensitive-data filters,
scope leases and deterministic consolidation. It remains disabled by default
(``memory.learn=false``), so startup and ordinary turns make no background
model calls unless the user opts in.
"""

from __future__ import annotations

from lumen.context.memory.extraction import (
    ExtractionProvenance,
    InMemoryMemoryWorkQueue,
    RawFact,
    SessionExtractionSource,
    SQLiteMemoryWorkQueue,
)
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
    "ExtractionProvenance",
    "InMemoryMemoryRepository",
    "InMemoryMemoryWorkQueue",
    "MemoryKind",
    "MemoryManager",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryScope",
    "MemorySource",
    "MemoryStatus",
    "RawFact",
    "SQLiteMemoryRepository",
    "SQLiteMemoryWorkQueue",
    "Sensitivity",
    "SessionExtractionSource",
    "render_memory_index",
    "render_topic",
]
