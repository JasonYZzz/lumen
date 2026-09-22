"""FTS5 full-text search index over spilled tool-output artifacts (derived cache).

The content-addressed artifact store and the session journal stay
authoritative; this index is a rebuildable projection on top of them. Deleting
``search.sqlite3`` degrades ``search_artifacts`` to empty hits and
checkpoint-episode retrieval to the lexical fallback, and the next
``ArtifactStore.store()`` lazily re-indexes on write.

Two chunk tables share ``(ref UNINDEXED, chunk_seq UNINDEXED, char_start
UNINDEXED, content)`` with different tokenizers, fused with RRF (K=60):

* ``artifact_chunks_porter``   - ``porter unicode61``: English stems and code
  identifiers (``configured`` matches ``configuration``).
* ``artifact_chunks_trigram``  - ``trigram``: substring and CJK matching
  (``useEff`` matches ``useEffect``). Skipped when the SQLite build lacks the
  trigram tokenizer (< 3.34); recall degrades, nothing breaks.

Chunking happens on the decoded ``str`` (same unit as ``read_artifact``
paging), so a hit's ``char_start`` doubles as a ``read_artifact(ref,
start=...)`` resume position. Validated checkpoint episodes get the same
dual-tokenizer treatment in the ``checkpoint_episodes_*`` tables, keyed
``(session_id, checkpoint_id)``. Reads never create the index file: with no
database present every search returns empty.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

#: The index lives next to the artifact blobs; ``mark_and_sweep`` only deletes
#: 64-hex blob names, so the database and its ``-wal``/``-shm`` are exempt.
INDEX_FILENAME = "search.sqlite3"

#: Chunk size on the decoded body, matching read_artifact's char paging unit.
DEFAULT_CHUNK_CHARS = 8_000
DEFAULT_CHUNK_OVERLAP_CHARS = 200

#: At most this many leading chars of one artifact are indexed; a pathological
#: 500 MB dump cannot stall the spill path. Hits on truncated bodies are
#: flagged so the model knows the tail was never indexed.
DEFAULT_MAX_INDEXED_CHARS = 64 * 1024 * 1024

_SNIPPET_WINDOW_CHARS = 300
_RRF_K = 60

_CHUNK_COLUMNS = "ref UNINDEXED, chunk_seq UNINDEXED, char_start UNINDEXED, content"
_EPISODE_COLUMNS = "session_id UNINDEXED, checkpoint_id UNINDEXED, ordinal UNINDEXED, content"
_PORTER_TABLE = "artifact_chunks_porter"
_TRIGRAM_TABLE = "artifact_chunks_trigram"
_EPISODE_PORTER_TABLE = "checkpoint_episodes_porter"
_EPISODE_TRIGRAM_TABLE = "checkpoint_episodes_trigram"

_T = TypeVar("_T")


class ArtifactSearchHit(BaseModel):
    """One chunk-level match: resume position plus a marked-up excerpt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    char_start: int = Field(ge=0)
    snippet: str
    #: True when the artifact exceeded the indexed-prefix cap, so content past
    #: the cap can never appear in hits.
    truncated: bool = False


class ArtifactSearchResult(BaseModel):
    """Canonical ``search_artifacts`` output contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    hits: list[ArtifactSearchHit]
    #: Model-facing guidance: read_artifact resume protocol on hits, query
    #: rewriting advice on empty results.
    hint: str


@dataclass(frozen=True, slots=True)
class _ChunkMatch:
    ref: str
    chunk_seq: int
    char_start: int
    marked: str


@dataclass(frozen=True, slots=True)
class _EpisodeMatch:
    checkpoint_id: str
    ordinal: int


class ArtifactSearchIndex:
    """SQLite FTS5 index over one artifact root (lazy, thread-safe).

    The connection is opened on first use with ``check_same_thread=False`` and
    guarded by an RLock, mirroring ``SQLiteMemoryRepository``. Construction is
    I/O-free; searches against a missing database return empty without
    creating anything. When the SQLite build lacks FTS5 entirely the index
    disables itself: writes become no-ops and searches return empty.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_indexed_chars: int = DEFAULT_MAX_INDEXED_CHARS,
    ) -> None:
        if max_indexed_chars <= 0:
            raise ValueError("max_indexed_chars must be positive")
        self._root = Path(root).expanduser()
        self._path = self._root / INDEX_FILENAME
        self._max_indexed_chars = max_indexed_chars
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._disabled = False
        self._trigram = False

    # -- connection ---------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        """Connect and apply the schema on first use (lazy file creation)."""

        if self._conn is None:
            self._root.mkdir(parents=True, exist_ok=True)
            os.chmod(self._root, 0o700)
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            try:
                os.chmod(self._path, 0o600)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_PORTER_TABLE} USING fts5("
                    f"{_CHUNK_COLUMNS}, tokenize='porter unicode61')"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS artifact_index_meta ("
                    "ref TEXT PRIMARY KEY, total_chars INTEGER NOT NULL, truncated INTEGER NOT NULL)"
                )
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_EPISODE_PORTER_TABLE} USING fts5("
                    f"{_EPISODE_COLUMNS}, tokenize='porter unicode61')"
                )
                try:
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TRIGRAM_TABLE} USING fts5("
                        f"{_CHUNK_COLUMNS}, tokenize='trigram')"
                    )
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_EPISODE_TRIGRAM_TABLE} USING fts5("
                        f"{_EPISODE_COLUMNS}, tokenize='trigram')"
                    )
                    # An existing table created by a trigram-capable build must
                    # still probe: the vtable only fails on use when the
                    # tokenizer is gone.
                    conn.execute(f"SELECT count(*) FROM {_TRIGRAM_TABLE}").fetchone()
                    self._trigram = True
                except sqlite3.OperationalError:
                    self._trigram = False
                conn.commit()
            except sqlite3.OperationalError:
                # No FTS5 in this SQLite build: degrade the whole index.
                conn.close()
                self._disabled = True
                raise
            self._conn = conn
        return self._conn

    def _writable(self) -> sqlite3.Connection | None:
        try:
            return self._db()
        except (OSError, sqlite3.OperationalError):
            return None

    def _readable(self) -> sqlite3.Connection | None:
        """Connect for reading only; never creates the database file."""

        if self._disabled or not self._path.is_file():
            return None
        try:
            return self._db()
        except (OSError, sqlite3.OperationalError):
            return None

    @property
    def trigram_available(self) -> bool:
        """Whether this SQLite build indexes the trigram tables (lazy connect)."""

        with self._lock:
            return self._writable() is not None and self._trigram

    # -- artifact chunks ------------------------------------------------------

    def index_artifact(self, ref: str, content: bytes | str) -> None:
        """Index ``content`` under ``ref`` (idempotent per content-addressed ref).

        Best-effort: any database failure rolls back and leaves the artifact
        readable but unsearchable; the blob write stays the primary path.
        """

        with self._lock:
            conn = self._writable()
            if conn is None:
                return
            try:
                row = conn.execute(
                    "SELECT ref FROM artifact_index_meta WHERE ref=?", (ref,)
                ).fetchone()
                if row is not None:
                    return
                text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
                total_chars = len(text)
                truncated = total_chars > self._max_indexed_chars
                if truncated:
                    text = text[: self._max_indexed_chars]
                chunks = list(_chunks(text))
                conn.executemany(
                    f"INSERT INTO {_PORTER_TABLE} (ref, chunk_seq, char_start, content) VALUES (?,?,?,?)",
                    [(ref, seq, start, chunk) for seq, (start, chunk) in enumerate(chunks)],
                )
                if self._trigram:
                    conn.executemany(
                        f"INSERT INTO {_TRIGRAM_TABLE} (ref, chunk_seq, char_start, content) "
                        "VALUES (?,?,?,?)",
                        [(ref, seq, start, chunk) for seq, (start, chunk) in enumerate(chunks)],
                    )
                conn.execute(
                    "INSERT INTO artifact_index_meta (ref, total_chars, truncated) VALUES (?,?,?)",
                    (ref, total_chars, 1 if truncated else 0),
                )
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()

    def remove_refs(self, refs: Iterable[str]) -> None:
        """Drop every chunk/meta row for ``refs`` (replay-safe when absent)."""

        targets = list(dict.fromkeys(refs))
        if not targets:
            return
        with self._lock:
            conn = self._readable()
            if conn is None:
                return
            marks = ",".join("?" for _ in targets)
            try:
                conn.execute(f"DELETE FROM {_PORTER_TABLE} WHERE ref IN ({marks})", targets)
                if self._trigram:
                    conn.execute(f"DELETE FROM {_TRIGRAM_TABLE} WHERE ref IN ({marks})", targets)
                conn.execute(f"DELETE FROM artifact_index_meta WHERE ref IN ({marks})", targets)
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()

    def purge_orphans(self) -> int:
        """Drop index rows whose artifact blob no longer exists on disk."""

        with self._lock:
            conn = self._readable()
            if conn is None:
                return 0
            try:
                rows = conn.execute("SELECT ref FROM artifact_index_meta").fetchall()
            except sqlite3.OperationalError:
                return 0
            orphans = [str(row["ref"]) for row in rows if not self._blob_present(str(row["ref"]))]
        self.remove_refs(orphans)
        return len(orphans)

    def _blob_present(self, ref: str) -> bool:
        hexpart = ref.removeprefix("sha256:")
        if len(hexpart) != 64 or not all(c in "0123456789abcdef" for c in hexpart):
            # Unparseable rows can never resolve to a blob; treat as orphans.
            return False
        return (self._root / hexpart).is_file()

    def search(self, query: str, *, limit: int = 5) -> list[ArtifactSearchHit]:
        """RRF-fused porter+trigram chunk hits, best first.

        A missing/disabled index or an unusable query returns empty; a query
        that only fails on one tokenizer table still gets the other table's
        ranking.
        """

        if limit <= 0:
            return []
        with self._lock:
            conn = self._readable()
            if conn is None:
                return []
            tokens = _fts_tokens(query)
            if not tokens:
                return []
            per_table = max(limit * 2, 10)
            ranked_lists: list[list[_ChunkMatch]] = []
            porter = self._match_chunks(
                conn, _PORTER_TABLE, _porter_query(tokens), limit=per_table
            )
            if porter is not None:
                ranked_lists.append(porter)
            if self._trigram:
                trigram_tokens = [token for token in tokens if len(token) >= 3]
                if trigram_tokens:
                    trigram = self._match_chunks(
                        conn, _TRIGRAM_TABLE, _phrase_query(trigram_tokens), limit=per_table
                    )
                    if trigram is not None:
                        ranked_lists.append(trigram)
            fused = _rrf_fuse(
                ranked_lists, key=lambda match: (match.ref, match.chunk_seq), limit=limit
            )
            truncated = self._truncated_refs(conn, {match.ref for match in fused})
            return [
                ArtifactSearchHit(
                    ref=match.ref,
                    char_start=match.char_start,
                    snippet=_snippet(match.marked),
                    truncated=match.ref in truncated,
                )
                for match in fused
            ]

    def _match_chunks(
        self, conn: sqlite3.Connection, table: str, fts_query: str, *, limit: int
    ) -> list[_ChunkMatch] | None:
        if not fts_query:
            return []
        try:
            rows = conn.execute(
                f"SELECT ref, chunk_seq, char_start, "
                f"highlight({table}, 3, char(2), char(3)) AS marked "
                f"FROM {table} WHERE {table} MATCH ? "
                f"ORDER BY bm25({table}, 1.0, 1.0, 1.0, 1.0) LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        return [
            _ChunkMatch(
                ref=str(row["ref"]),
                chunk_seq=int(row["chunk_seq"]),
                char_start=int(row["char_start"]),
                marked=str(row["marked"]),
            )
            for row in rows
        ]

    def _truncated_refs(self, conn: sqlite3.Connection, refs: set[str]) -> set[str]:
        if not refs:
            return set()
        targets = sorted(refs)
        marks = ",".join("?" for _ in targets)
        try:
            rows = conn.execute(
                f"SELECT ref FROM artifact_index_meta WHERE truncated=1 AND ref IN ({marks})",
                targets,
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row["ref"]) for row in rows}

    # -- checkpoint episodes ----------------------------------------------------

    def upsert_episode(self, session_id: str, checkpoint_id: str, ordinal: int, content: str) -> None:
        """(Re)index one validated checkpoint summary, keyed (session, checkpoint)."""

        with self._lock:
            conn = self._writable()
            if conn is None:
                return
            tables = [_EPISODE_PORTER_TABLE, *([_EPISODE_TRIGRAM_TABLE] if self._trigram else [])]
            try:
                for table in tables:
                    conn.execute(
                        f"DELETE FROM {table} WHERE session_id=? AND checkpoint_id=?",
                        (session_id, checkpoint_id),
                    )
                    conn.execute(
                        f"INSERT INTO {table} (session_id, checkpoint_id, ordinal, content) "
                        "VALUES (?,?,?,?)",
                        (session_id, checkpoint_id, ordinal, content),
                    )
                conn.commit()
            except sqlite3.OperationalError:
                conn.rollback()

    def search_episodes(self, session_id: str, query: str, *, limit: int) -> list[str] | None:
        """FTS-ranked checkpoint ids for ``session_id``; ``None`` asks for fallback.

        ``None`` (rather than empty) means the index could not answer at all —
        missing database, no usable tokens, or every tokenizer table erroring —
        so the caller should use its lexical scoring instead.
        """

        if limit <= 0:
            return []
        with self._lock:
            conn = self._readable()
            if conn is None:
                return None
            tokens = _fts_tokens(query)
            if not tokens:
                return None
            per_table = max(limit * 2, 10)
            ranked_lists: list[list[_EpisodeMatch]] = []
            porter = self._match_episodes(
                conn, _EPISODE_PORTER_TABLE, session_id, _porter_query(tokens), limit=per_table
            )
            if porter is not None:
                ranked_lists.append(porter)
            if self._trigram:
                trigram_tokens = [token for token in tokens if len(token) >= 3]
                if trigram_tokens:
                    trigram = self._match_episodes(
                        conn,
                        _EPISODE_TRIGRAM_TABLE,
                        session_id,
                        _phrase_query(trigram_tokens),
                        limit=per_table,
                    )
                    if trigram is not None:
                        ranked_lists.append(trigram)
            if not ranked_lists:
                return None
            fused = _rrf_fuse(ranked_lists, key=lambda match: match.checkpoint_id, limit=limit)
            return [match.checkpoint_id for match in fused]

    def _match_episodes(
        self,
        conn: sqlite3.Connection,
        table: str,
        session_id: str,
        fts_query: str,
        *,
        limit: int,
    ) -> list[_EpisodeMatch] | None:
        if not fts_query:
            return []
        try:
            rows = conn.execute(
                f"SELECT checkpoint_id, ordinal FROM {table} "
                f"WHERE session_id = ? AND {table} MATCH ? "
                f"ORDER BY bm25({table}, 1.0, 1.0, 1.0, 1.0), ordinal DESC LIMIT ?",
                (session_id, fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        return [
            _EpisodeMatch(
                checkpoint_id=str(row["checkpoint_id"]),
                ordinal=int(row["ordinal"]),
            )
            for row in rows
        ]


def _fts_tokens(query: str) -> list[str]:
    """Whitespace tokens with FTS5 syntax characters stripped (injection-safe)."""

    tokens = (raw.replace('"', "").replace("*", "").strip() for raw in query.split())
    return [token for token in tokens if token]


def _porter_query(tokens: Sequence[str]) -> str:
    """OR-ed prefix terms for the porter table (recall first, rank decides)."""

    return " OR ".join(f'"{token}"*' for token in tokens)


def _phrase_query(tokens: Sequence[str]) -> str:
    """OR-ed quoted phrases for the trigram table (no prefix operator there)."""

    return " OR ".join(f'"{token}"' for token in tokens)


def _chunks(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> Iterable[tuple[int, str]]:
    """Yield ``(char_start, chunk)`` windows over the decoded body."""

    if not text:
        return
    step = max(1, size - overlap)
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        yield start, text[start:end]
        if end == len(text):
            break
        start += step


def _snippet(marked: str, *, window: int = _SNIPPET_WINDOW_CHARS) -> str:
    """~``window`` chars either side of the first hit, markers as ``**...**``."""

    anchor = marked.find("\x02")
    if anchor < 0:
        return marked[:window].replace("\x02", "").replace("\x03", "")
    start = max(0, anchor - window)
    excerpt = marked[start : anchor + window]
    return excerpt.replace("\x02", "**").replace("\x03", "**")


def _rrf_fuse(
    ranked_lists: Sequence[Sequence[_T]],
    *,
    key: Callable[[_T], object],
    limit: int,
) -> list[_T]:
    """Reciprocal Rank Fusion (K=60) over per-tokenizer rankings."""

    scores: dict[object, float] = {}
    best: dict[object, tuple[int, _T]] = {}
    for candidates in ranked_lists:
        for rank, candidate in enumerate(candidates):
            identity = key(candidate)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (_RRF_K + rank)
            current = best.get(identity)
            if current is None or rank < current[0]:
                best[identity] = (rank, candidate)
    ordered = sorted(best.items(), key=lambda item: -scores[item[0]])
    return [candidate for _, (_, candidate) in ordered[:limit]]


__all__ = [
    "DEFAULT_MAX_INDEXED_CHARS",
    "INDEX_FILENAME",
    "ArtifactSearchHit",
    "ArtifactSearchIndex",
    "ArtifactSearchResult",
]
