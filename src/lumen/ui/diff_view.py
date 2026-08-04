"""Shared unified-diff rendering for tool cards and the approval panel.

Both surfaces show the same model-requested file changes, so the diff
generation and red/green coloring live here once. Uses stdlib
:mod:`difflib` (no new dependency): additions render in the theme's
``success`` color, removals in ``error``, context and ``@@`` hunk headers
dim — the universal diff convention.
"""

from __future__ import annotations

import difflib

from rich.text import Text

#: Bounds so a pathological write_file/edit_file call cannot flood the
#: timeline or the approval panel with an unbounded diff.
MAX_DIFF_CHARS = 100_000
MAX_DIFF_LINES = 400


def unified_diff_lines(old: str, new: str, *, context_lines: int = 1) -> list[str]:
    """Return headerless unified diff lines for ``old`` -> ``new``.

    The ``---``/``+++`` file headers are dropped: they carry no useful info
    for an in-place edit and add visual noise to compact surfaces. Output is
    capped at :data:`MAX_DIFF_LINES` lines with a trailing truncation marker.
    """

    raw = difflib.unified_diff(
        old[:MAX_DIFF_CHARS].splitlines(),
        new[:MAX_DIFF_CHARS].splitlines(),
        n=context_lines,
        lineterm="",
    )
    lines = [line for line in raw if not line.startswith("---") and not line.startswith("+++")]
    if len(lines) > MAX_DIFF_LINES:
        lines = [*lines[:MAX_DIFF_LINES], f"… diff truncated after {MAX_DIFF_LINES} lines"]
    return lines


def style_diff_lines(lines: list[str], *, add_color: str, del_color: str) -> Text:
    """Colorize headerless diff lines into a rich ``Text``.

    Each line becomes a separately colored segment: ``+`` additions in
    ``add_color``, ``-`` removals in ``del_color``, ``@@`` hunk headers dim
    italic, everything else (context, truncation markers) dim.
    """

    text = Text()
    for line in lines:
        text.append(line + "\n", style=_line_style(line, add_color=add_color, del_color=del_color))
    return text


def style_diff_text(diff: str, *, add_color: str, del_color: str) -> Text:
    """Colorize an already-rendered unified diff string.

    Unlike :func:`style_diff_lines`, the input may still carry ``---``/``+++``
    file headers (e.g. the approval presenter's payload); they are kept dim
    rather than mistaken for removals/additions.
    """

    text = Text()
    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            style = "dim"
        else:
            style = _line_style(line, add_color=add_color, del_color=del_color)
        text.append(line + "\n", style=style)
    return text


def _line_style(line: str, *, add_color: str, del_color: str) -> str:
    """Map one diff line to its style token."""

    if line.startswith("@@"):
        return "dim italic"
    if line.startswith("+"):
        return add_color
    if line.startswith("-"):
        return del_color
    return "dim"


__all__ = [
    "MAX_DIFF_CHARS",
    "MAX_DIFF_LINES",
    "style_diff_lines",
    "style_diff_text",
    "unified_diff_lines",
]
