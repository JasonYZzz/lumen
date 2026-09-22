"""Tests for the content-addressed artifact store (M3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lumen.context.artifacts import ArtifactStore, ArtifactStoreError


def _store(tmp_path: Path, *, inline: int = 32) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", inline_threshold_bytes=inline)


def test_store_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref1 = store.store("hello world")
    ref2 = store.store("hello world")  # same content -> same ref, no rewrite
    assert ref1 == ref2
    assert ref1.startswith("sha256:")
    assert store.read(ref1) == b"hello world"


def test_store_creates_files_with_private_permissions(tmp_path: Path) -> None:
    """Artifact files are 0600 so other users cannot read tool-output bodies."""

    store = _store(tmp_path)
    ref = store.store(b"secret build log")
    path = tmp_path / "artifacts" / ref.split(":", 1)[1]
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_store_rejects_invalid_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactStoreError):
        store.read("../escape")  # type: ignore[arg-type]
    with pytest.raises(ArtifactStoreError):
        store.read("sha256:notinhex!")
    with pytest.raises(ArtifactStoreError):
        store.read("notsha256:abc")


def test_spill_returns_none_for_small_content(tmp_path: Path) -> None:
    store = _store(tmp_path, inline=100)
    assert store.spill(b"small") is None  # below threshold -> inline
    ref = store.spill(b"x" * 100)
    assert ref is not None and ref.startswith("sha256:")


def test_spill_never_policy_does_not_persist(tmp_path: Path) -> None:
    """Secret-bearing outputs (policy=never) are never written to disk."""

    store = _store(tmp_path, inline=1)
    ref = store.spill(b"super secret" * 100, artifact_policy="never")
    assert ref is None
    # No artifact files were created.
    assert not (tmp_path / "artifacts").exists() or not list((tmp_path / "artifacts").glob("*"))


def test_refcount_cleanup_removes_zero_ref_artifact(tmp_path: Path) -> None:
    """An artifact is removed only when its last holder releases it."""

    store = _store(tmp_path, inline=1)
    ref = store.store(b"big body" * 100)
    store.add_hold(ref, "session-1")
    store.add_hold(ref, "checkpoint-1")
    # Releasing one holder leaves the artifact (two holders).
    assert store.release_hold(ref, "session-1") is False
    assert store.hold_count(ref) == 1
    assert store.read(ref) is not None
    # Releasing the last holder removes the file.
    assert store.release_hold(ref, "checkpoint-1") is True
    assert store.read(ref) is None


def test_add_hold_is_idempotent_per_holder(tmp_path: Path) -> None:
    """Re-adding the same holder does not inflate the count (replay safety)."""

    store = _store(tmp_path)
    ref = store.store(b"x")
    store.add_hold(ref, "s1")
    store.add_hold(ref, "s1")  # idempotent
    assert store.hold_count(ref) == 1


def test_release_unknown_hold_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = store.store(b"x")
    # Never added -> no-op, artifact remains.
    assert store.release_hold(ref, "never-added") is False
    assert store.read(ref) is not None


# --------------------------------------------------------------------------- #
# Search index linkage (derived cache on the same root)
# --------------------------------------------------------------------------- #


def test_store_indexes_body_and_sweep_removes_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = store.store(b"indexable spilled build log body")
    assert store.search_index.search("indexable") != []
    assert store.mark_and_sweep(set()) == (ref,)
    assert store.search_index.search("indexable") == []


def test_sweep_preserves_the_index_file(tmp_path: Path) -> None:
    """mark_and_sweep only deletes 64-hex blobs; search.sqlite3 is exempt."""

    store = _store(tmp_path)
    ref = store.store(b"kept body survives the sweep")
    removed = store.mark_and_sweep({ref})
    assert removed == ()
    assert (tmp_path / "artifacts" / "search.sqlite3").is_file()
    assert store.search_index.search("kept body") != []


def test_index_is_a_derived_cache_rebuilt_on_store(tmp_path: Path) -> None:
    """Deleting search.sqlite3 degrades search; the next store() re-indexes."""

    store = _store(tmp_path)
    ref = store.store(b"reindex me after deletion")
    index_file = tmp_path / "artifacts" / "search.sqlite3"
    assert index_file.is_file()
    index_file.unlink()

    # A fresh store on the same root sees no index and creates nothing on read.
    fresh = _store(tmp_path)
    assert fresh.search_index.search("reindex") == []
    assert fresh.store(b"reindex me after deletion") == ref
    assert fresh.search_index.search("reindex") != []
