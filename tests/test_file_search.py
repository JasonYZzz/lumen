"""Tests for the tree-wide ``@path`` file search.

Validates the pi/tui-ported behaviour: full-tree walk when no slash is present,
scoped walk when the user names a directory, ``.git`` exclusion, scoring
weights, and top-N truncation.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lumen.ui.autocomplete import CompletionSuggestion
from lumen.ui.file_search import FileHit, FileSearchHandle, build_suggestion, search_files


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Build a realistic project tree for search tests."""

    (tmp_path / "src" / "lumen" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "lumen" / "ui" / "app.py").write_text("x")
    (tmp_path / "src" / "lumen" / "ui" / "autocomplete.py").write_text("x")
    (tmp_path / "src" / "lumen" / "runtime.py").write_text("x")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tui.py").write_text("x")
    (tmp_path / "README.md").write_text("r")
    (tmp_path / "app.py").write_text("root app")
    # .git must be present but never surfaced.
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / ".git" / "config").write_text("[core]")
    return tmp_path


# ---------------------------------------------------------------------------
# Tree-wide search (no slash in query)
# ---------------------------------------------------------------------------


def test_finds_deep_file_by_name(workspace: Path) -> None:
    """``@app`` surfaces files named ``app.py`` at any depth."""

    hits = search_files("@app", workspace)
    names = {h.name for h in hits}
    assert "app.py" in names
    # Both the root app.py and the nested one should appear.
    rel_paths = {h.rel_path for h in hits}
    assert "app.py" in rel_paths
    assert "src/lumen/ui/app.py" in rel_paths


def test_git_directory_is_excluded(workspace: Path) -> None:
    """Nothing under ``.git`` ever appears in suggestions."""

    hits = search_files("@", workspace)
    for h in hits:
        assert not h.rel_path.startswith(".git"), f"unexpected .git hit: {h.rel_path}"
        assert "/.git/" not in h.rel_path
    # And a direct query for git internals comes back empty.
    assert search_files("@HEAD", workspace) == []
    assert search_files("@config", workspace) == []


def test_symlink_targets_outside_workspace_are_not_suggested(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "external").symlink_to(outside, target_is_directory=True)

    assert search_files("@secret", tmp_path) == []
    assert search_files("@external/", tmp_path) == []


def test_empty_query_returns_everything_sorted(workspace: Path) -> None:
    """``@`` alone lists the whole tree (directories first by score)."""

    hits = search_files("@", workspace)
    # We should see at least the top-level entries plus a few deep ones.
    names = {h.name for h in hits}
    assert "README.md" in names
    assert "app.py" in names
    assert "src" in names  # directory
    # Directories get a score bonus so they outrank files when the query
    # is empty (tie-break: alphabetical).
    first_few_scores = [h.score for h in hits[:3]]
    assert all(s > 1 for s in first_few_scores if any(h.is_dir for h in hits[:3]))


# ---------------------------------------------------------------------------
# Scoped search (slash in query → restrict to named directory)
# ---------------------------------------------------------------------------


def test_slash_scopes_to_named_directory(workspace: Path) -> None:
    """``@src/`` restricts results to descendants of ``src/``."""

    hits = search_files("@src/", workspace)
    rel_paths = {h.rel_path for h in hits}
    # Everything is under src/.
    assert all(p.startswith("src/") for p in rel_paths)
    # The nested app.py surfaces because we walk src/ recursively.
    assert "src/lumen/ui/app.py" in rel_paths
    # Root-level files are NOT included (out of scope).
    assert "app.py" not in rel_paths
    assert "README.md" not in rel_paths


def test_slash_with_fragment_filters_within_scope(workspace: Path) -> None:
    """``@src/lumen/ui/app`` finds the deep app.py via scoping."""

    hits = search_files("@src/lumen/ui/app", workspace)
    names = [h.name for h in hits]
    assert "app.py" in names


def test_nonexistent_directory_falls_back_to_tree(workspace: Path) -> None:
    """If the named directory doesn't exist, the slash acts as a separator."""

    # ``@nonexistent/foo`` — ``nonexistent`` isn't a dir, so we fall back to
    # whole-tree search filtering on the trailing fragment ``foo``.
    hits = search_files("@nonexistent/app", workspace)
    # Tree search still finds app.py by name, despite the bogus dir prefix.
    names = [h.name for h in hits]
    assert "app.py" in names


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_suggestion
# ---------------------------------------------------------------------------


def test_build_suggestion_file_gets_trailing_space() -> None:
    hit = FileHit(rel_path="src/app.py", name="app.py", is_dir=False, score=80)
    sug = build_suggestion(hit, "@app")
    assert isinstance(sug, CompletionSuggestion)
    assert sug.label == "app.py"
    assert sug.insert == "@src/app.py "  # trailing space closes completion
    assert sug.description == "src/app.py"


def test_build_suggestion_directory_gets_trailing_slash() -> None:
    hit = FileHit(rel_path="src", name="src", is_dir=True, score=90)
    sug = build_suggestion(hit, "@sr")
    assert sug.label == "src/"
    assert sug.insert == "@src/"  # trailing slash re-triggers completion
    assert not sug.insert.endswith(" ")


# ---------------------------------------------------------------------------
# fd-backed behaviour (gitignore respect, dot-literal, cache exclusion)
# ---------------------------------------------------------------------------


def test_dot_is_literal_not_separator(workspace: Path) -> None:
    """``@agent.md`` matches only filenames literally containing ``agent.md``.

    This mirrors pi/tui exactly: dots are NOT path separators and NOT fuzzy
    wildcards. If no file is named ``agent.md`` the result is empty — the user
    should type ``@agent`` to find ``agent.yaml``.
    """

    # No file named agent.md exists → empty.
    assert search_files("@nonexistent.md", workspace) == []
    # A file whose name literally contains the dotted substring matches.
    (workspace / "config.test.md").write_text("x")
    hits = search_files("@config.test.md", workspace)
    assert any(h.name == "config.test.md" for h in hits)


def test_venv_directory_is_excluded_via_gitignore(tmp_path: Path) -> None:
    """``.venv/`` is suppressed when it's in the project's ``.gitignore``.

    This is the core regression test for the ``@ex`` bug: a vendored venv can
    hold thousands of files matching short queries, drowning the real project
    files. fd respects ``.gitignore`` (when invoked correctly); the os.walk
    fallback prunes ``.venv`` via the hardcoded denylist.
    """

    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "exceptions.py").write_text("x")
    (tmp_path / "examples").mkdir()
    (tmp_path / ".gitignore").write_text(".venv/\n")
    # ``examples`` is the real hit we want; ``.venv/.../exceptions.py`` must
    # not appear even though it also matches ``ex``.
    hits = search_files("@ex", tmp_path)
    rel_paths = {h.rel_path for h in hits}
    assert "examples" in rel_paths
    assert not any(".venv" in p for p in rel_paths), f".venv leaked into results: {rel_paths}"


def test_pycache_is_excluded(workspace: Path) -> None:
    """``__pycache__`` and ``.pyc`` files never appear in suggestions."""

    (workspace / "src" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "__pycache__" / "app.cpython-313.pyc").write_text("x")
    (workspace / "src" / "app.py").write_text("x")
    hits = search_files("@app", workspace)
    rel_paths = {h.rel_path for h in hits}
    assert "src/app.py" in rel_paths
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in rel_paths), (
        f"cache artefacts leaked: {rel_paths}"
    )


def test_top_n_caps_displayed_results(workspace: Path) -> None:
    """No more than ``top_n`` (default 20) suggestions are returned."""

    for i in range(40):
        (workspace / f"cap_{i:02d}.py").write_text("x")
    hits = search_files("@cap", workspace)
    assert len(hits) <= 20


def test_exact_match_scores_highest(workspace: Path) -> None:
    """An exact filename match outranks prefix and substring matches."""

    (workspace / "app").write_text("x")  # exact match for query "app"
    (workspace / "app.py").write_text("x")  # prefix match
    (workspace / "my_app.py").write_text("x")  # substring match
    hits = search_files("@app", workspace)
    # The exact ``app`` file should be at or near the top.
    assert hits[0].name == "app"
    assert hits[0].score >= 100


def test_file_search_handle_cancels_inflight_search(workspace: Path) -> None:
    started = threading.Event()

    def slow_search(
        _prefix: str,
        _root: Path,
        *,
        cancel_event: threading.Event,
    ) -> list[FileHit]:
        started.set()
        cancel_event.wait(timeout=1)
        return []

    handle = FileSearchHandle("@slow", workspace, search=slow_search)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(handle.run)
        assert started.wait(timeout=1)
        handle.cancel()
        assert future.result(timeout=1) == []
