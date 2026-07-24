"""Memory manager: recall, remember, forget (M5).

The manager is the engine's surface for durable memory (plan §11.4, §11.5).
``remember`` is the synchronous explicit-write path (``/memory remember``); the
two-stage auto extraction/consolidation is M6 and is not wired here. ``recall``
is the two-step load: a fixed high-confidence index plus a lexical query for
the current prompt, both bounded so memory cannot crowd the context window.

Conflict and tombstone handling live in the repository; the manager only
decides scope, builds records and tracks use. ``memory.use=False`` disables
recall entirely (no memory blocks this turn) - the privacy switch (plan §11.6).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from lumen.context.memory.records import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    Sensitivity,
)
from lumen.context.memory.repository import MemoryRepository


class MemoryManager:
    """Recall, remember and forget durable memory (plan §11.4, §11.5)."""

    def __init__(self, repository: MemoryRepository, *, use: bool = True) -> None:
        self._repo = repository
        self.use = use

    # -- explicit writes --------------------------------------------------

    def remember(
        self,
        content: str,
        *,
        scope: MemoryScope = MemoryScope.PROJECT,
        kind: MemoryKind = MemoryKind.PREFERENCE,
        confidence: float = 1.0,
        session_id: str | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        path_glob: str | None = None,
    ) -> MemoryRecord:
        """Persist an explicit memory (``/memory remember``).

        The id is derived from scope + content so re-remembering the same fact
        updates it in place rather than creating a duplicate. The repository
        refuses to resurrect a forgotten id (tombstone invariant).
        """

        now = datetime.now(UTC)
        record = MemoryRecord(
            id=self._record_id(scope, content),
            scope=scope,
            kind=kind,
            content=content,
            source_session_ids=(session_id,) if session_id else (),
            source_kind=MemorySource.EXPLICIT,
            confidence=confidence,
            created_at=now,
            updated_at=now,
            valid_from=now,
            status=MemoryStatus.ACTIVE,
            sensitivity=sensitivity,
            path_glob=path_glob,
        )
        return self._repo.remember(record)

    def forget(self, target: str) -> int:
        """Forget by id or by content substring; returns the count tombstoned.

        A substring match writes tombstones for every matching active record, so
        ``/memory forget <topic>`` cannot be evaded by an id the user does not
        know. Tombstones prevent a replayed extraction from reviving the fact.
        """

        if self._repo.get(target) is not None:
            return 1 if self._repo.forget(target) else 0
        count = 0
        for record in self._repo.list(include_forgotten=False):
            if target.lower() in record.content.lower():
                if self._repo.forget(record.id):
                    count += 1
        return count

    # -- recall -----------------------------------------------------------

    def recall(
        self,
        prompt: str,
        *,
        scope: MemoryScope | None = None,
        path: str | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        """Two-step recall: fixed index + lexical query (plan §11.5).

        Returns ``[]`` when ``use`` is disabled (the privacy switch). Each
        recalled record is marked used so use_count/last_used_at feed ranking.
        """

        if not self.use:
            return []
        hits = self._repo.query(prompt, scope=scope, path=path, limit=limit)
        for record in hits:
            self._repo.mark_used(record.id)
        return hits

    def index(self) -> list[MemoryRecord]:
        """The fixed high-confidence index loaded into every prompt (plan §11.5)."""

        if not self.use:
            return []
        return self._repo.index()

    def list(self, scope: MemoryScope | None = None) -> list[MemoryRecord]:
        return self._repo.list(scope=scope, include_forgotten=False)

    # -- internal ---------------------------------------------------------

    @staticmethod
    def _record_id(scope: MemoryScope, content: str) -> str:
        digest = hashlib.sha256(f"{scope.value}:{content}".encode()).hexdigest()
        return f"mem-{digest[:16]}"


__all__ = ["MemoryManager"]
