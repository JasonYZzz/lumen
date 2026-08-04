"""Deterministic stage-two memory consolidation under durable scope leases."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from lumen.context.memory.extraction import ExtractionCandidate, MemoryWorkQueue
from lumen.context.memory.records import MemoryRecord, MemoryScope, MemorySource, memory_record_id
from lumen.context.memory.repository import MemoryRepository


class Consolidator:
    """Merge candidates without network, shell, MCP or collaborator access."""

    def __init__(
        self,
        repository: MemoryRepository,
        work_queue: MemoryWorkQueue,
        *,
        lease_seconds: float = 30.0,
    ) -> None:
        self._repo = repository
        self._work_queue = work_queue
        self._lease_seconds = lease_seconds
        self._owner = f"consolidator-{uuid4().hex}"

    def consolidate(self, candidates: Sequence[ExtractionCandidate]) -> list[MemoryRecord]:
        by_scope: dict[MemoryScope, list[ExtractionCandidate]] = {}
        for candidate in candidates:
            by_scope.setdefault(candidate.scope, []).append(candidate)

        stored: list[MemoryRecord] = []
        for scope, group in by_scope.items():
            if not self._work_queue.acquire_scope(
                scope,
                self._owner,
                lease_seconds=self._lease_seconds,
            ):
                raise RuntimeError(f"memory scope {scope.value!r} is leased")
            try:
                seen: set[str] = set()
                for candidate in group:
                    key = candidate.content.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    stored.append(self._store(candidate))
            finally:
                self._work_queue.release_scope(scope, self._owner)
        return stored

    def _store(self, candidate: ExtractionCandidate) -> MemoryRecord:
        now = datetime.now(UTC)
        record = MemoryRecord(
            id=memory_record_id(
                candidate.scope,
                candidate.content,
                project_id=candidate.project_id,
            ),
            scope=candidate.scope,
            kind=candidate.kind,
            content=candidate.content,
            source_session_ids=(candidate.source_session_id,),
            source_event_ids=candidate.source_event_ids,
            source_kind=candidate.source_kind,
            confidence=candidate.confidence,
            created_at=now,
            updated_at=now,
            valid_from=now,
            valid_until=(
                now + timedelta(days=180)
                if candidate.source_kind is MemorySource.OBSERVED
                else now + timedelta(days=90)
            ),
            project_id=candidate.project_id,
        )
        return self._repo.remember(record)


__all__ = ["Consolidator"]
