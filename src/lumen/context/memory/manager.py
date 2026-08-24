"""Durable memory facade plus the M6 background learning pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias

import yaml
from pydantic import TypeAdapter, ValidationError

from lumen.context.memory.consolidation import Consolidator
from lumen.context.memory.extraction import (
    DEFAULT_MIN_SESSION_TURNS,
    InMemoryMemoryWorkQueue,
    MemoryWorkQueue,
    RawFact,
    SessionExtractionSource,
    extract_candidates,
    is_eligible_session,
)
from lumen.context.memory.projection import render_memory_index, render_topic
from lumen.context.memory.records import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    Sensitivity,
    memory_record_id,
)
from lumen.context.memory.redaction import contains_secret
from lumen.context.memory.repository import MemoryRepository

RawFactExtractor: TypeAlias = Callable[
    [SessionExtractionSource],
    Sequence[RawFact] | Awaitable[Sequence[RawFact]],
]
SessionSourceLoader: TypeAlias = Callable[[str], SessionExtractionSource | None]


class MemoryManager:
    """The only public seam for recall, explicit writes and auto-learning."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        project_id: str | None = None,
        use: bool = True,
        learn: bool = False,
        extractor: RawFactExtractor | None = None,
        source_loader: SessionSourceLoader | None = None,
        work_queue: MemoryWorkQueue | None = None,
        min_session_turns: int = DEFAULT_MIN_SESSION_TURNS,
        max_attempts: int = 5,
        retry_base_seconds: float = 2.0,
        idle_seconds: float = 120.0,
        projection_dir: str | Path | None = None,
    ) -> None:
        self._repo = repository
        self.project_id = project_id
        self.use = use
        self.learn = learn
        self.incognito = False
        self._extractor = extractor
        self._source_loader = source_loader
        self._work_queue = work_queue or InMemoryMemoryWorkQueue()
        self._consolidator = Consolidator(repository, self._work_queue)
        self._min_session_turns = min_session_turns
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._idle_seconds = idle_seconds
        self._projection_dir = Path(projection_dir).expanduser() if projection_dir is not None else None
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._drain_lock = asyncio.Lock()

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
        content = content.strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        if contains_secret(content):
            raise ValueError("memory contains sensitive credential material")
        now = datetime.now(UTC)
        record = MemoryRecord(
            id=memory_record_id(scope, content, project_id=self.project_id),
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
            project_id=None if scope is MemoryScope.USER else self.project_id,
        )
        stored = self._repo.remember(record)
        self.rebuild_projection()
        return stored

    def forget(self, target: str) -> int:
        direct = self._repo.get(target)
        if direct is not None and self._is_visible(direct):
            count = 1 if self._repo.forget(target) else 0
            if count:
                self.rebuild_projection()
            return count
        count = 0
        for record in self.list():
            if target.casefold() in record.content.casefold() and self._repo.forget(record.id):
                count += 1
        if count:
            self.rebuild_projection()
        return count

    def export_edit(self, target: str) -> tuple[str, Path | None]:
        """Export one visible record as a private, validated Markdown draft."""

        record = self._editable_record(target)
        draft = self._render_edit_draft(record, record.content)
        path = self._draft_path(record.id)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            self._atomic_write(path, draft)
        return draft, path

    def apply_edit(self, target: str, *, draft: str | None = None) -> MemoryRecord:
        """Validate a draft and mutate the authoritative record in place."""

        existing = self._editable_record(target)
        path = self._draft_path(existing.id)
        consume_path = False
        if draft is None:
            if path is None or not path.is_file() or path.is_symlink():
                raise ValueError("memory edit draft is missing or unsafe; export it first")
            draft = path.read_text(encoding="utf-8")
            consume_path = True
        metadata, content = self._parse_edit_draft(draft)
        if str(metadata.get("id", "")) != existing.id:
            raise ValueError("memory edit draft id does not match its target")
        if str(metadata.get("scope", "")) != existing.scope.value:
            raise ValueError("memory edit cannot change scope; remember a new record instead")
        content = content.strip()
        if not content:
            raise ValueError("memory content cannot be empty")
        if contains_secret(content):
            raise ValueError("memory contains sensitive credential or personal material")
        try:
            kind = MemoryKind(str(metadata.get("kind", existing.kind.value)))
            confidence_value = metadata.get("confidence", existing.confidence)
            if not isinstance(confidence_value, (str, int, float)):
                raise ValueError
            confidence = float(confidence_value)
        except (TypeError, ValueError) as error:
            raise ValueError("memory edit has invalid kind or confidence") from error
        path_glob_value = metadata.get("path_glob", existing.path_glob)
        path_glob = str(path_glob_value).strip() if path_glob_value is not None else None
        if existing.scope is MemoryScope.PATH and not path_glob:
            raise ValueError("PATH-scope memory requires a path_glob")
        updated = existing.model_copy(
            update={
                "content": content,
                "kind": kind,
                "confidence": confidence,
                "path_glob": path_glob,
                "status": MemoryStatus.ACTIVE,
                "updated_at": datetime.now(UTC),
            }
        )
        # Re-validate fields changed via model_copy before touching authority.
        updated = MemoryRecord.model_validate(updated.model_dump())
        stored = self._repo.remember(updated)
        self.rebuild_projection()
        if consume_path and path is not None:
            path.unlink(missing_ok=True)
        return stored

    def edit_content(self, target: str, content: str) -> MemoryRecord:
        """Apply a direct TUI edit through the same Markdown validation path."""

        record = self._editable_record(target)
        return self.apply_edit(target, draft=self._render_edit_draft(record, content))

    # -- recall -----------------------------------------------------------

    def recall(
        self,
        prompt: str,
        *,
        scope: MemoryScope | None = None,
        path: str | None = None,
        limit: int = 20,
        exclude_ids: Collection[str] = (),
    ) -> list[MemoryRecord]:
        if not self.use or self.incognito:
            return []
        hits = self._repo.query(
            prompt,
            scope=scope,
            path=path,
            project_id=self.project_id,
            limit=limit,
        )
        hits = [record for record in hits if record.id not in exclude_ids]
        for record in hits:
            self._repo.mark_used(record.id)
        return hits

    def index(self) -> list[MemoryRecord]:
        if not self.use or self.incognito:
            return []
        return self._repo.index(project_id=self.project_id)

    def list(self, scope: MemoryScope | None = None) -> list[MemoryRecord]:
        return self._repo.list(
            scope=scope,
            include_forgotten=False,
            project_id=self.project_id,
        )

    # -- automatic learning ----------------------------------------------

    def configure_learning(
        self,
        *,
        extractor: RawFactExtractor | None = None,
        source_loader: SessionSourceLoader | None = None,
    ) -> None:
        """Update runtime-owned adapters without replacing durable state."""

        if extractor is not None:
            self._extractor = extractor
        if source_loader is not None:
            self._source_loader = source_loader

    def set_incognito(self, enabled: bool) -> None:
        self.incognito = enabled
        if enabled:
            for task in self._idle_tasks.values():
                task.cancel()
            self._idle_tasks.clear()
            self._work_queue.cancel_pending()

    def schedule_session(self, session_id: str, *, idle_seconds: float | None = None) -> bool:
        """Queue the latest eligible snapshot after an idle debounce."""

        if not self.learn or self.incognito or self._source_loader is None or self._extractor is None:
            return False
        source = self._source_loader(session_id)
        if source is None or not is_eligible_session(
            turn_count=source.turn_count,
            incognito=source.incognito,
            has_stable_result=source.has_stable_result,
            min_turns=self._min_session_turns,
        ):
            return False
        job = self._work_queue.enqueue(session_id, source.digest)
        if job is None:
            return False
        previous = self._idle_tasks.pop(session_id, None)
        if previous is not None:
            previous.cancel()
        delay = self._idle_seconds if idle_seconds is None else idle_seconds
        task = asyncio.create_task(self._run_after_idle(session_id, delay))
        self._idle_tasks[session_id] = task
        return True

    async def _run_after_idle(self, session_id: str, idle_seconds: float) -> None:
        try:
            if idle_seconds > 0:
                await asyncio.sleep(idle_seconds)
            await self.drain_pending()
        finally:
            current = self._idle_tasks.get(session_id)
            if current is asyncio.current_task():
                self._idle_tasks.pop(session_id, None)

    async def drain_pending(self) -> None:
        """Recover and process every currently-due outbox job."""

        if not self.learn or self.incognito or self._source_loader is None or self._extractor is None:
            return
        async with self._drain_lock:
            while job := self._work_queue.claim(lease_seconds=60.0):
                try:
                    source = self._source_loader(job.session_id)
                    if source is None or source.digest != job.transcript_digest:
                        # A newer snapshot superseded this job or the session was removed.
                        self._work_queue.complete(job.id)
                        continue
                    if not is_eligible_session(
                        turn_count=source.turn_count,
                        incognito=source.incognito,
                        has_stable_result=source.has_stable_result,
                        min_turns=self._min_session_turns,
                    ):
                        self._work_queue.complete(job.id)
                        continue
                    proposed = self._extractor(source)
                    facts = await proposed if inspect.isawaitable(proposed) else proposed
                    candidates = extract_candidates(
                        list(facts),
                        session_id=source.session_id,
                        provenance=source.provenance,
                        project_id=self.project_id,
                    )
                    if candidates:
                        await asyncio.to_thread(self._consolidator.consolidate, candidates)
                        await asyncio.to_thread(self.rebuild_projection)
                    self._work_queue.complete(job.id)
                except asyncio.CancelledError:
                    if self.incognito:
                        self._work_queue.cancel_pending(job.session_id)
                    else:
                        self._work_queue.fail(
                            job.id,
                            "cancelled",
                            base_delay_seconds=0.0,
                            max_attempts=self._max_attempts,
                        )
                    raise
                except Exception as error:
                    self._work_queue.fail(
                        job.id,
                        str(error),
                        base_delay_seconds=self._retry_base_seconds,
                        max_attempts=self._max_attempts,
                    )

    async def wait_for_idle(self) -> None:
        tasks = tuple(self._idle_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

    def start(self) -> None:
        """Resume due crash-recovered jobs without blocking startup."""

        if not self.learn or self.incognito or self._extractor is None or self._source_loader is None:
            return
        if "__recovery__" in self._idle_tasks:
            return
        task = asyncio.create_task(self._run_after_idle("__recovery__", 0.0))
        self._idle_tasks["__recovery__"] = task

    def learning_status(self) -> dict[str, object]:
        return {
            "use": self.use,
            "learn": self.learn,
            "incognito": self.incognito,
            "queue": self._work_queue.stats(),
            "idle_sessions": sorted(self._idle_tasks),
            "projection_dir": str(self._projection_dir) if self._projection_dir is not None else None,
        }

    def rebuild_projection(self) -> str:
        """Atomically rebuild MEMORY.md and kind topic views from authority."""

        records = self.list()
        index = render_memory_index(records)
        if self._projection_dir is None:
            return index
        self._projection_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._projection_dir, 0o700)
        self._atomic_write(self._projection_dir / "MEMORY.md", index)
        topics_dir = self._projection_dir / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(topics_dir, 0o700)
        by_kind: dict[MemoryKind, list[MemoryRecord]] = {}
        for record in records:
            by_kind.setdefault(record.kind, []).append(record)
        for kind, kind_records in by_kind.items():
            self._atomic_write(
                topics_dir / f"{kind.value}.md",
                render_topic(kind_records, title=f"Memory: {kind.value}"),
            )
        expected_topics = {f"{kind.value}.md" for kind in by_kind}
        for kind in MemoryKind:
            stale = topics_dir / f"{kind.value}.md"
            if stale.name not in expected_topics:
                stale.unlink(missing_ok=True)
        return index

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def close(self) -> None:
        tasks = tuple(self._idle_tasks.values())
        self._idle_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self._work_queue, "close", None)
        if close is not None:
            close()
        close_repo = getattr(self._repo, "close", None)
        if close_repo is not None:
            close_repo()

    def _is_visible(self, record: MemoryRecord) -> bool:
        return record.scope is MemoryScope.USER or record.project_id == self.project_id

    def _editable_record(self, target: str) -> MemoryRecord:
        record = self._repo.get(target)
        if record is None or not self._is_visible(record) or record.status is MemoryStatus.FORGOTTEN:
            raise ValueError(f"memory record {target!r} is not editable")
        return record

    def _draft_path(self, record_id: str) -> Path | None:
        if self._projection_dir is None:
            return None
        digest = hashlib.sha256(record_id.encode()).hexdigest()[:16]
        return self._projection_dir / ".edits" / f"{digest}.md"

    @staticmethod
    def _render_edit_draft(record: MemoryRecord, content: str) -> str:
        metadata = {
            "id": record.id,
            "scope": record.scope.value,
            "kind": record.kind.value,
            "confidence": record.confidence,
            "path_glob": record.path_glob,
        }
        frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{frontmatter}\n---\n{content.strip()}\n"

    @staticmethod
    def _parse_edit_draft(draft: str) -> tuple[dict[str, object], str]:
        lines = draft.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("memory edit draft must start with YAML frontmatter")
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration as error:
            raise ValueError("memory edit draft has unclosed YAML frontmatter") from error
        try:
            raw = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError as error:
            raise ValueError("memory edit contains invalid YAML frontmatter") from error
        try:
            metadata = TypeAdapter(dict[str, object]).validate_python(raw)
        except ValidationError as error:
            raise ValueError("memory edit frontmatter must be an object") from error
        allowed = {"id", "scope", "kind", "confidence", "path_glob"}
        unknown = set(metadata) - allowed
        if unknown:
            raise ValueError(f"memory edit has unknown fields: {sorted(unknown)}")
        return metadata, "\n".join(lines[end + 1 :])


__all__ = ["MemoryManager", "RawFactExtractor", "SessionSourceLoader"]
