"""Shared constants used across lumen modules.

These are kept here rather than scattered across modules to ensure they stay
in sync. Currently holds the canonical set of directories that should never
be scanned for files or skills — vendored dependencies, build artefacts, VCS
metadata, and Python caches.
"""

from __future__ import annotations

#: Directories pruned during filesystem scans (file search, skill discovery).
#: These are never useful as ``@path`` or skill targets and can contain
#: thousands of files that drown real results.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        ".tox",
        ".eggs",
        "site-packages",
    }
)

__all__ = ["IGNORED_DIRS"]
