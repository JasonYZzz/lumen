"""Tests for the FTS5 artifact search index (derived cache over the store)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lumen.context.artifact_index import INDEX_FILENAME, ArtifactSearchIndex
from lumen.context.artifacts import ArtifactStore


def _index(tmp_path: Path, **kwargs: int) -> ArtifactSearchIndex:
    return ArtifactSearchIndex(tmp_path / "artifacts", **kwargs)


def _ref(char: str) -> str:
    return "sha256:" + char * 64


def _porter_row_count(tmp_path: Path, ref: str) -> int:
    path = tmp_path / "artifacts" / INDEX_FILENAME
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT count(*) FROM artifact_chunks_porter WHERE ref=?", (ref,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_search_without_database_returns_empty_and_creates_nothing(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assert index.search("anything") == []
    assert index.search_episodes("session", "anything", limit=3) is None
    assert index.purge_orphans() == 0
    assert not (tmp_path / "artifacts").exists()


def test_porter_stemming_matches_inflected_english(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ref = _ref("a")
    index.index_artifact(ref, "the configuration was tuned for production deployment")
    hits = index.search("configured")
    assert [hit.ref for hit in hits] == [ref]
    assert "**configuration**" in hits[0].snippet
    assert hits[0].char_start == 0
    assert hits[0].truncated is False


def test_substring_match_via_trigram(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ref = _ref("b")
    index.index_artifact(ref, "const hook = useEffect(() => mount(), [])")
    # A mid-token substring only the trigram table can recall.
    hits = index.search("Effect")
    if index.trigram_available:
        assert [hit.ref for hit in hits] == [ref]
        assert "**Effect**" in hits[0].snippet
    else:
        assert hits == []
    # The plan's headline example works through either tokenizer.
    assert [hit.ref for hit in index.search("useEff")] == [ref]


def test_cjk_substring_match_via_trigram(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ref = _ref("c")
    index.index_artifact(ref, "部署配置完成, 数据库迁移成功, 等待回滚窗口")
    hits = index.search("数据库迁移")
    if index.trigram_available:
        assert [hit.ref for hit in hits] == [ref]
        assert "**数据库迁移" in hits[0].snippet
    else:
        assert hits == []


def test_index_artifact_is_idempotent_per_ref(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ref = _ref("d")
    index.index_artifact(ref, "hello world")
    index.index_artifact(ref, "hello world")
    assert _porter_row_count(tmp_path, ref) == 1


def test_remove_refs_clears_index_rows(tmp_path: Path) -> None:
    index = _index(tmp_path)
    ref = _ref("e")
    index.index_artifact(ref, "removable unique token")
    assert index.search("removable") != []
    index.remove_refs([ref])
    assert index.search("removable") == []
    assert _porter_row_count(tmp_path, ref) == 0


def test_snippet_centers_on_hit_and_char_start_resumes_reading(tmp_path: Path) -> None:
    index = _index(tmp_path)
    filler = "filler sentence padding the body. " * 200
    body = filler + "NEEDLE_UNIQUE_TERM appears mid body" + filler
    ref = _ref("f")
    index.index_artifact(ref, body)

    hit = index.search("NEEDLE_UNIQUE_TERM")[0]
    assert hit.ref == ref
    assert "**NEEDLE_UNIQUE_TERM**" in hit.snippet
    # The ~300 char window either side puts the first marker near the middle.
    assert hit.snippet.find("**NEEDLE_UNIQUE_TERM**") == 300
    # char_start is a read_artifact-style resume position covering the hit.
    assert "NEEDLE_UNIQUE_TERM" in body[hit.char_start : hit.char_start + 8000]


def test_index_cap_truncates_and_flags_hits(tmp_path: Path) -> None:
    index = ArtifactSearchIndex(tmp_path / "artifacts", max_indexed_chars=1_000)
    body = "earlyunique " * 100 + "lateneedle " * 200
    ref = _ref("0")
    index.index_artifact(ref, body)

    early = index.search("earlyunique")
    assert [hit.ref for hit in early] == [ref]
    assert early[0].truncated is True
    # Content past the cap was never indexed.
    assert index.search("lateneedle") == []


def test_search_with_no_usable_tokens_degrades(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.index_artifact(_ref("1"), "some body")
    assert index.search("") == []
    assert index.search('"*') == []
    assert index.search_episodes("session", '"', limit=3) is None


def test_store_hooks_index_and_reconcile(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", inline_threshold_bytes=1)
    ref = store.store("unique token zqxwv in a spilled body")
    assert store.search_index.search("zqxwv") != []

    removed = store.mark_and_sweep(set())
    assert removed == (ref,)
    assert store.search_index.search("zqxwv") == []


def test_release_hold_removes_index_rows(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.store("held body until the last holder releases")
    store.add_hold(ref, "session-1")
    assert store.search_index.search("held body") != []
    assert store.release_hold(ref, "session-1") is True
    assert store.search_index.search("held body") == []


def test_purge_orphans_reconciles_missing_blobs(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.store("orphan candidate body")
    blob = tmp_path / "artifacts" / ref.split(":", 1)[1]
    blob.unlink()
    assert store.search_index.purge_orphans() == 1
    assert store.search_index.search("orphan") == []


def test_episode_upsert_search_and_session_isolation(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.upsert_episode("s1", "cp1", 0, "goals: migrate the configuration database")
    index.upsert_episode("s1", "cp2", 1, "goals: bakery hydration ratios")
    index.upsert_episode("s2", "cp3", 0, "configuration for another session")

    # Porter stemming finds what the lexical bigram/term scoring could not.
    assert index.search_episodes("s1", "configured", limit=3) == ["cp1"]
    assert index.search_episodes("s1", "bakery", limit=3) == ["cp2"]
    assert index.search_episodes("s2", "configured", limit=3) == ["cp3"]

    # Re-upsert is idempotent (keyed by session + checkpoint).
    index.upsert_episode("s1", "cp1", 0, "goals: migrate the configuration database")
    assert index.search_episodes("s1", "configured", limit=3) == ["cp1"]

    # No database yet -> None, asking the caller for the lexical fallback.
    fresh = ArtifactSearchIndex(tmp_path / "fresh")
    assert fresh.search_episodes("s1", "configured", limit=3) is None
