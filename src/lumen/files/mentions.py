"""Expand ``@path`` mentions independently from any user-interface toolkit.

This is the Lumen counterpart to pi/tui's ``@`` mention picker, with one
key difference: instead of leaving the model to call ``read_file`` on the
referenced path, we expand the file contents inline before the prompt reaches
the model. That saves a tool round-trip and matches the user's actual intent
("show this file to the model").

The expansion is defensive: every path is resolved through ``Workspace``, so
escapes and missing files become ``<file … missing>…</file>`` annotations
rather than raising — the user's prompt is never aborted by a bad mention.
"""

from __future__ import annotations

import re
from pathlib import Path

from lumen.tools.workspace import Workspace, WorkspaceViolation

# Reuse the same output ceiling as the capability tools so a runaway @mention
# on a 100 MiB log file can't blow up the prompt.
_MAX_CONTENT_BYTES = 64 * 1024

# Match an @-mention of a path. We accept:
#   @"quoted path"     — explicit quoting for paths with spaces
#   @path/with/slashes — bare relative or absolute path
#
# The bare form requires the @ to be at a token boundary (start of text, or
# preceded by whitespace / an opening bracket) — this avoids matching the
# host part of emails (``user@example.com``). The path body is then validated
# in code: a bare all-alpha word like ``@here`` is treated as a Slack-style
# mention and left alone; anything with a path indicator (``.``, ``/``, ``~``,
# ``-``) is treated as a real file reference.
#
# Trailing sentence punctuation (``.``, ``,``, ``;``) on a bare mention is
# stripped in post-processing — see ``_strip_trailing_punct``.
_QUOTED_MENTION = re.compile(r'@"(?P<qpath>[^"]*)"')
_BARE_MENTION = re.compile(r"(?:(?<=\s)|(?<=^)|(?<=[\(\[\{]))@(?P<bpath>[A-Za-z0-9_./~\-][A-Za-z0-9_./~\-]*)")

# A bare mention must contain at least one of these to be treated as a path
# rather than a Slack handle. ``@here`` → mention, ``@x.txt`` → path.
_PATH_INDICATORS = ".", "/", "~", "-"


def _looks_like_path(body: str) -> bool:
    return any(c in body for c in _PATH_INDICATORS)


def _read_bounded(path: Path) -> tuple[str, bool]:
    """Return ``(content, truncated)`` for ``path``, capped at the byte ceiling."""

    # Read one byte beyond the limit so large files never need to be loaded in
    # full merely to decide whether a truncation marker is required.
    with path.open("rb") as handle:
        raw = handle.read(_MAX_CONTENT_BYTES + 1)
    sample = raw[:_MAX_CONTENT_BYTES]
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    if b"\x00" in sample or (sample and not decoded):
        size = path.stat().st_size
        return f"[binary file omitted: {size} bytes; content was not injected]", False
    if len(raw) <= _MAX_CONTENT_BYTES:
        return decoded, False
    kept = sample.decode("utf-8", errors="ignore")
    return (
        f"{kept}\n[content truncated at {_MAX_CONTENT_BYTES} bytes]",
        True,
    )


def _render_file_block(path_str: str, *, missing: str | None = None) -> str:
    """Build the ``<file>`` block for either a found or missing mention."""

    if missing is not None:
        return f'<file path="{path_str}" missing>{missing}</file>'
    return f'<file path="{path_str}">'


def _expand_one(path_str: str, workspace: Workspace) -> str:
    """Resolve and read one mention path, returning the replacement block."""

    try:
        resolved = workspace.resolve(path_str)
    except WorkspaceViolation as error:
        return _render_file_block(path_str, missing=f"escapes workspace: {error}")
    except (ValueError, OSError) as error:
        return _render_file_block(path_str, missing=f"invalid path: {error}")

    if not resolved.exists():
        return _render_file_block(path_str, missing="not found")
    if resolved.is_dir():
        # Directories aren't inlined as content — list their immediate
        # children so the model knows what's there without us recursively
        # dumping a huge tree.
        try:
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir())
            listing = "\n".join(entries[:200])
            if len(entries) > 200:
                listing += f"\n[… {len(entries) - 200} more entries truncated]"
            return f'<file path="{path_str}" directory>\n{listing}\n</file>'
        except OSError as error:
            return _render_file_block(path_str, missing=f"unreadable directory: {error}")

    try:
        content, truncated = _read_bounded(resolved)
    except OSError as error:
        return _render_file_block(path_str, missing=f"unreadable: {error}")

    suffix = " [truncated]" if truncated else ""
    return f'<file path="{path_str}"{suffix}>\n{content}\n</file>'


def _strip_trailing_punct(path: str) -> tuple[str, str]:
    """Split trailing punctuation (``.``, ``,``, ``)``, ``]``) off a bare path.

    Returns ``(path_body, trailing)``. Users naturally write ``see @x.txt.``
    or ``(@foo.py)``; the punctuation isn't part of the filename.
    """

    # Strip sentence punctuation that almost never ends a real filename.
    # We keep ``/`` because a trailing slash is meaningful (marks a directory).
    stripped = ""
    while path and path[-1] in (".", ",", ";"):
        stripped = path[-1] + stripped
        path = path[:-1]
    return path, stripped


def expand_file_mentions(text: str, workspace: Workspace) -> str:
    """Replace every ``@path`` mention in ``text`` with an inline file block.

    See module docstring for the format and the safety contract. This function
    never raises: malformed or escaping mentions become ``<file … missing>``
    annotations so the prompt still goes through.
    """

    if not text or "@" not in text:
        return text

    def _replace_bare(match: re.Match[str]) -> str:
        path = match.group("bpath")
        body, trailing = _strip_trailing_punct(path)
        # Bare all-alpha tokens (``@here``, ``@team``) are Slack-style
        # mentions, not file references — leave them alone.
        if not _looks_like_path(body):
            return f"@{path}"
        return _expand_one(body, workspace) + trailing

    def _replace_quoted(match: re.Match[str]) -> str:
        path = match.group("qpath")
        return _expand_one(path, workspace)

    # Quoted mentions first (so the quotes are consumed), then bare ones.
    text = _QUOTED_MENTION.sub(_replace_quoted, text)
    text = _BARE_MENTION.sub(_replace_bare, text)
    return text


__all__ = ["expand_file_mentions"]
