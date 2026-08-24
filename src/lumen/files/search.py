"""UI-independent tree-wide file search for ``@path`` completion.

This is the Lumen port of pi/tui's ``autocomplete.ts`` file suggestions.
Pi shells out to ``fd`` (a Rust ripgrep-equivalent for filenames); we do the
same when ``fd`` is on PATH, and fall back to a streaming ``os.walk`` that
mirrors fd's semantics as closely as possible without external deps.

Three behavioural pillars copied from pi (verified against
``pi/packages/tui/src/autocomplete.ts``):

1. **``fd`` first.** It respects ``.gitignore`` by default, prunes
   ``node_modules`` / ``.venv`` / ``dist`` for free, walks in parallel, and
   early-terminates on ``--max-results``. This is the only way to take a
   972-entry vendored tree down to a useful 20.
2. **Two-stage cap.** Collect up to ``_MAX_MATCHES = 100`` raw matches, score
   each by filename similarity, sort, return the top ``_TOP_N = 20``.
3. **Single-axis scoring, dot-as-literal.** Exact filename = 100, filename
   startswith = 80, filename contains = 50, full path contains = 30, +10 for
   directories. The empty query (``@`` alone) gives every entry score 1 and
   preserves walk order. Dots are NOT separators — ``@agent.md`` matches only
   filenames literally containing ``agent.md``, exactly like pi.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lumen.completion import CompletionSuggestion
from lumen.constants import IGNORED_DIRS

#: Hard cap on raw matches collected before scoring. Pi passes
#: ``--max-results 100`` to ``fd``; the os.walk fallback stops at the same
#: number of *matches* (not entries walked — that was the bug that capped
#: ``@ex`` at 2 results in a project with 972 matching files).
_MAX_MATCHES = 100

#: How many suggestions survive scoring and sorting. Pi slices to 20.
_TOP_N = 20

#: Score weights — copied verbatim from pi's ``scoreEntry`` at
#: ``autocomplete.ts:697-717``. Single axis, no depth or length penalty.
_SCORE_EXACT = 100
_SCORE_PREFIX = 80
_SCORE_CONTAINS = 50
_SCORE_PATH_CONTAINS = 30
_SCORE_DIR_BONUS = 10

#: Directories the os.walk fallback prunes in-place. ``fd`` gets this for free
#: via ``.gitignore``; the fallback needs an explicit denylist to avoid
#: drowning in vendored / build artefacts. Shared with skill discovery via
#: ``lumen.constants.IGNORED_DIRS``.
_PRUNED_DIRS = IGNORED_DIRS


@dataclass(frozen=True, slots=True)
class FileHit:
    """One candidate from a file search, ready to be turned into a suggestion."""

    rel_path: str  # POSIX path relative to the workspace root
    name: str  # basename, no trailing slash
    is_dir: bool
    score: int


def _score(name: str, rel_path: str, query: str, is_dir: bool) -> int:
    """Return pi's match-quality score for one entry.

    ``query`` is the trailing fragment after the last ``/`` (or the whole
    query if there is no slash), lowercased. Empty query → score 1 for every
    entry (preserves walk order). An entry that doesn't substring-match the
    query scores 0 and is dropped by the caller.

    Note: dots are literal. ``agent.md`` only matches filenames containing the
    literal substring ``agent.md`` — this mirrors pi exactly. Pi does NOT split
    on dots; neither do we.
    """

    if not query:
        # Empty query (user typed just ``@``): every entry is equally valid.
        # Pi assigns score 1 to all and preserves fd's walk order. We give
        # directories a small bonus so they surface first for navigation — a
        # deliberate, minor deviation that matches user expectations better
        # than fd's filesystem-ordering noise.
        return _SCORE_DIR_BONUS if is_dir else 1

    lower_name = name.lower()
    lower_query = query.lower()
    if lower_name == lower_query:
        score = _SCORE_EXACT
    elif lower_name.startswith(lower_query):
        score = _SCORE_PREFIX
    elif lower_query in lower_name:
        score = _SCORE_CONTAINS
    elif lower_query in rel_path.lower():
        score = _SCORE_PATH_CONTAINS
    else:
        return 0
    if is_dir:
        score += _SCORE_DIR_BONUS
    return score


def _split_query(raw: str) -> tuple[str, str]:
    """Split a trigger token into ``(dir_part, trailing_fragment)``.

    Mirrors pi's ``resolveScopedFuzzyQuery``: only ``/`` is a separator.
    ``@src/com`` → ``("src", "com")``; ``@src/`` → ``("src", "")``;
    ``@app`` → ``("", "app")``; ``@agent.md`` → ``("", "agent.md")`` (dot is
    NOT a separator). The leading ``@`` and any leading quote are stripped.
    """

    query = raw[1:] if raw.startswith("@") else raw
    if query.startswith('"'):
        query = query[1:]
    if "/" not in query:
        return "", query
    dir_part, _, frag = query.rpartition("/")
    return dir_part, frag


def _looks_like_git_path(rel_path: str) -> bool:
    """Whether ``rel_path`` is or lives under ``.git`` (defensive filter)."""

    return rel_path == ".git" or rel_path.startswith(".git/") or "/.git/" in rel_path


def _is_confined(path: Path, root: Path) -> bool:
    """Reject symlinks and scoped paths whose resolved target leaves ``root``."""

    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# fd-backed search (preferred — pi parity)
# ---------------------------------------------------------------------------


def _fd_available() -> bool:
    """Whether ``fd`` is on PATH. Cached implicitly per-process via shutil."""

    return shutil.which("fd") is not None


def _run_fd(
    base: Path,
    fd_query: str,
    *,
    full_path: bool,
    cancel_event: threading.Event | None = None,
) -> list[tuple[str, str, bool]]:
    """Invoke ``fd`` and return ``(rel_path, name, is_dir)`` tuples.

    Mirrors pi's ``walkDirectoryWithFd`` argument list: ``--max-results 100``,
    both types, follow symlinks, show hidden, exclude ``.git`` three ways. We
    rely on fd's built-in ``.gitignore`` respect (the default, no flag needed)
    to prune ``node_modules`` / ``.venv`` / etc.
    """

    args: list[str] = [
        "fd",
        # IMPORTANT: ``--base-directory`` must be ``.`` (relative), NOT an
        # absolute path. fd 10.x only honours ``.gitignore`` when the search
        # root is the current working directory; an absolute
        # ``--base-directory`` is treated as a foreign path and ``.venv/``,
        # ``node_modules/`` etc. leak in. We pass ``.`` and set ``cwd=base``
        # on the subprocess so fd reads the project's ``.gitignore``.
        "--base-directory",
        ".",
        "--max-results",
        str(_MAX_MATCHES),
        "--type",
        "f",
        "--type",
        "d",
        "--follow",
        # NOTE: we deliberately do NOT pass ``--hidden``. fd's ``--hidden``
        # mode surfaces ``.venv/`` and other hidden venv/build dirs even when
        # they're in ``.gitignore`` — the resulting noise drowns real
        # results. Without ``--hidden``, fd still respects ``.gitignore``
        # and skips dotfiles by default. Users who want a hidden file (e.g.
        # ``.env``) that isn't gitignored can still find it; ``.venv/`` and
        # ``.git/`` are correctly suppressed. This is a deliberate deviation
        # from pi, which uses ``--hidden`` but relies on fd's gitignore
        # handling that we found unreliable for hidden dirs in fd 10.4.
        "--exclude",
        ".git",
        "--exclude",
        ".git/*",
        "--exclude",
        ".git/**",
        # ``__pycache__`` is in every Python project's ``.gitignore`` but fd
        # 10.4 doesn't reliably honour the trailing-slash glob form
        # ``__pycache__/`` — so we exclude it explicitly. ``.pyc`` follows for
        # the same reason: compiled artefacts are never useful ``@`` targets.
        "--exclude",
        "__pycache__",
        "--exclude",
        "__pycache__/*",
        "--exclude",
        "*.pyc",
    ]
    if full_path:
        args.append("--full-path")
    if fd_query:
        # Pi passes the raw query; fd treats it as a regex. We escape regex
        # metacharacters except ``/`` so the user's literal query wins — pi's
        # ``buildFdPathQuery`` already does this for the slash-bearing branch,
        # and the no-slash branch in pi passes the raw string (where dots act
        # as regex wildcards). We escape to make dots literal, matching the
        # ``scoreEntry`` contract above.
        args.append(re.escape(fd_query).replace("\\/", "/"))

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # fd respects ``.gitignore`` relative to its CWD, not the search
            # path. If we don't set cwd=base, fd runs from the Python
            # process's cwd (often the user's home or a temp test dir) and
            # never finds the project's ``.gitignore`` — so ``.venv/`` and
            # ``node_modules/`` leak in and drown the real results.
            cwd=str(base),
        )
        deadline = time.monotonic() + 2.0
        while True:
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() >= deadline:
                proc.terminate()
                proc.communicate()
                return []
            try:
                stdout, _stderr = proc.communicate(timeout=0.025)
                break
            except subprocess.TimeoutExpired:
                continue
    except FileNotFoundError:
        return []

    if proc.returncode != 0:
        return []

    out: list[tuple[str, str, bool]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # fd prints paths relative to --base-directory. A trailing slash on
        # fd output marks a directory; otherwise we stat (cheap, fd already
        # classified via --type).
        is_dir = line.endswith("/")
        rel = line.rstrip("/")
        if _looks_like_git_path(rel):
            continue
        name = os.path.basename(rel)
        out.append((rel, name, is_dir))
    return out


# ---------------------------------------------------------------------------
# os.walk fallback (no fd installed)
# ---------------------------------------------------------------------------


def _walk_fallback(
    base: Path,
    query_frag: str,
    cancel_event: threading.Event | None = None,
) -> list[tuple[str, str, bool]]:
    """Streaming ``os.walk`` fallback when ``fd`` is unavailable.

    Stops at ``_MAX_MATCHES`` *matches* (not entries walked — that was the
    previous bug). Prunes ``_PRUNED_DIRS`` in place to keep vendored /
    build dirs from drowning the result set. We can't cheaply respect
    project-local ``.gitignore`` without the ``pathspec`` dependency, so the
    denylist is conservative but explicit.
    """

    # Compile a case-insensitive substring filter once. Empty query → None,
    # meaning "accept everything".
    if query_frag:
        needle = query_frag.lower()
    else:
        needle = None

    matches: list[tuple[str, str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(base, topdown=True):
        if cancel_event is not None and cancel_event.is_set():
            return []
        # Prune in place — os.walk won't descend into removed entries.
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        for name in dirnames:
            if _matches(name, needle):
                full = Path(dirpath) / name
                try:
                    rel = full.relative_to(base).as_posix()
                except ValueError:
                    continue
                matches.append((rel, name, True))
                if len(matches) >= _MAX_MATCHES:
                    return matches
        for name in filenames:
            if name in _PRUNED_DIRS:
                continue
            if _matches(name, needle):
                full = Path(dirpath) / name
                try:
                    rel = full.relative_to(base).as_posix()
                except ValueError:
                    continue
                matches.append((rel, name, False))
                if len(matches) >= _MAX_MATCHES:
                    return matches
    return matches


def _matches(name: str, needle: str | None) -> bool:
    """Substring match for the fallback walker. ``None`` = accept all."""

    if needle is None:
        return True
    return needle in name.lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search_files(
    prefix: str,
    root: Path,
    *,
    top_n: int = _TOP_N,
    cancel_event: threading.Event | None = None,
) -> list[FileHit]:
    """Return scored, sorted file hits matching ``prefix`` under ``root``.

    ``prefix`` is the editor's trigger token including the leading ``@``
    (e.g. ``"@app"``, ``"@src/com"``). See module docstring for the
    scoped-vs-tree decision and the fd/os.walk strategy.
    """

    dir_part, frag = _split_query(prefix)

    # Scoped mode: the user named a real directory. List its contents
    # recursively so deep files under it surface. If the named directory
    # doesn't exist, fall through to tree mode — the slash then acts as a
    # path-segment separator in the filter.
    scoped_root: Path | None = None
    if dir_part:
        candidate = root / dir_part
        if candidate.is_dir() and _is_confined(candidate, root):
            scoped_root = candidate

    walk_root = scoped_root if scoped_root is not None else root
    if not walk_root.is_dir():
        return []

    # Decide which walker to use. ``fd`` is preferred (pi parity); os.walk is
    # the always-available fallback.
    if _fd_available():
        # fd takes a regex pattern, not a substring. We pass the trailing
        # fragment (after the last slash) so scoped searches filter on the
        # right thing; --full-path makes the regex match the whole relative
        # path when the user typed a slash (matches pi's logic at
        # autocomplete.ts:150-152).
        full_path = "/" in dir_part
        entries = _run_fd(walk_root, frag, full_path=full_path, cancel_event=cancel_event)
    else:
        entries = _walk_fallback(walk_root, frag, cancel_event)

    # Score every entry; drop zeros (no match). When scoped, the rel_path we
    # score against is relative to the *workspace root* (not the scoped dir)
    # so the suggestion's display path stays correct.
    hits: list[FileHit] = []
    for rel, name, is_dir in entries:
        if scoped_root is not None and dir_part:
            display_rel = f"{dir_part}/{rel}" if rel else dir_part
        else:
            display_rel = rel
        if _looks_like_git_path(display_rel):
            continue
        if not _is_confined(root / display_rel, root):
            continue
        score = _score(name, display_rel, frag, is_dir)
        if score <= 0:
            continue
        hits.append(FileHit(rel_path=display_rel, name=name, is_dir=is_dir, score=score))

    # Sort: score desc, then alphabetical by rel_path for stable ordering.
    hits.sort(key=lambda h: (-h.score, h.rel_path))
    return hits[:top_n]


class FileSearchHandle:
    """A cancellable file-completion query, including its ``fd`` process."""

    def __init__(
        self,
        prefix: str,
        root: Path,
        *,
        search: Callable[..., list[FileHit]] = search_files,
    ) -> None:
        self.prefix = prefix
        self.root = root
        self._search = search
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> list[FileHit]:
        try:
            return self._search(self.prefix, self.root, cancel_event=self._cancelled)
        except TypeError as error:
            # Preserve compatibility with small injected two-argument search
            # fakes while production searches use the cancellation token.
            if "cancel_event" not in str(error):
                raise
            return self._search(self.prefix, self.root)


def build_suggestion(hit: FileHit, prefix: str) -> CompletionSuggestion:
    """Turn a ``FileHit`` into a ``CompletionSuggestion`` for the dropdown.

    Directory inserts keep a trailing ``/`` so the picker re-triggers for the
    next path segment; file inserts get a trailing space to close completion
    and let the user keep typing their prompt.
    """

    suffix = "/" if hit.is_dir else " "
    insert = f"@{hit.rel_path}{suffix}"
    label = hit.name + ("/" if hit.is_dir else "")
    return CompletionSuggestion(label=label, insert=insert, description=hit.rel_path)


__all__ = ["FileHit", "FileSearchHandle", "build_suggestion", "search_files"]
