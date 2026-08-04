from __future__ import annotations

import codecs
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TypeAlias

from lumen.constants import IGNORED_DIRS
from lumen.tools.capability import build_capability_specs
from lumen.tools.spec import Risk, ToolSpec
from lumen.tools.workspace import Workspace, WorkspaceViolation

MAX_OUTPUT_BYTES = 64 * 1024
_BINARY_SAMPLE_BYTES = 8 * 1024

ReadFileResult: TypeAlias = dict[str, str | int | bool | None]


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    kept = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}\n[output truncated at {MAX_OUTPUT_BYTES} bytes]"


def _iter_search_files(base: Path, glob: str) -> Iterator[Path]:
    """Yield search candidates without descending into ignored directories."""

    if glob != "**/*":
        for candidate in sorted(base.glob(glob)):
            relative = candidate.relative_to(base)
            if any(part in IGNORED_DIRS for part in relative.parts[:-1]):
                continue
            yield candidate
        return

    candidates: list[Path] = []
    for current, directory_names, file_names in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_DIRS and not (current_path / name).is_symlink()
        )
        candidates.extend(current_path / name for name in file_names)
    yield from sorted(candidates)


def build_builtin_specs(root: str | Path, *, max_timeout: float = 60.0) -> list[ToolSpec]:
    """Return the read-only builtins followed by the capability tools.

    Read tools are unconditional; the capability tools (write/edit/run) join the
    registry and go through the standard permission path so the model can only
    execute them after approval by default.
    """

    workspace = Workspace(root)

    def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> ReadFileResult:
        """Read one bounded page of a UTF-8 workspace file with continuation metadata.

        The result contains ``content``, ``start_line``, ``end_line``,
        ``lines_returned``, ``has_more``, ``next_start_line``, ``total_lines``
        (when EOF was reached), and ``truncated_reason``. Continue a partial
        result by calling ``read_file`` again with ``start_line`` set to
        ``next_start_line``.
        """
        if start_line < 1 or max_lines < 1:
            raise ValueError("start_line and max_lines must be positive")
        resolved = workspace.resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {path}")

        # Reject obvious binary data before creating a text decoder. This is a
        # bounded probe rather than a full-file pre-read, so a small page never
        # causes a large file to be loaded into memory.
        with resolved.open("rb") as binary_file:
            sample = binary_file.read(_BINARY_SAMPLE_BYTES)
        if b"\x00" in sample:
            raise ValueError(f"binary file is not supported by read_file: {path}")
        try:
            # ``final=False`` permits the bounded sample to end in the middle
            # of an otherwise valid multi-byte code point.
            codecs.getincrementaldecoder("utf-8")(errors="strict").decode(sample, final=False)
        except UnicodeDecodeError as error:
            raise ValueError(f"file is not valid UTF-8 text: {path}") from error

        rendered_lines: list[str] = []
        content_bytes = 0
        last_seen_line = 0
        has_more = False
        next_start_line: int | None = None
        truncated_reason: str | None = None

        try:
            with resolved.open("r", encoding="utf-8", errors="strict", newline=None) as text_file:
                line_number = 0
                while True:
                    if len(rendered_lines) >= max_lines:
                        if text_file.read(1):
                            has_more = True
                            next_start_line = line_number + 1
                            truncated_reason = "line_limit"
                        break

                    # Bound the allocation for one pathological line. TextIO's
                    # size is in characters, so the retained object is at most
                    # roughly 4x the byte budget for valid UTF-8 instead of an
                    # arbitrarily large minified line.
                    raw_line = text_file.readline(MAX_OUTPUT_BYTES + 1)
                    if raw_line == "":
                        break
                    line_number += 1
                    last_seen_line = line_number
                    if line_number < start_line:
                        # Drain the remainder of an overlong skipped line in
                        # bounded chunks without retaining it.
                        while len(raw_line) == MAX_OUTPUT_BYTES + 1 and not raw_line.endswith("\n"):
                            raw_line = text_file.readline(MAX_OUTPUT_BYTES + 1)
                        continue

                    line = raw_line.removesuffix("\n")
                    rendered = f"{line_number}: {line}"
                    separator_bytes = 1 if rendered_lines else 0
                    rendered_bytes = len(rendered.encode("utf-8"))
                    line_continues = len(raw_line) == MAX_OUTPUT_BYTES + 1 and not raw_line.endswith("\n")
                    if content_bytes + separator_bytes + rendered_bytes > MAX_OUTPUT_BYTES:
                        if not rendered_lines:
                            raise ValueError(
                                f"line {line_number} exceeds the read_file output limit of "
                                f"{MAX_OUTPUT_BYTES} bytes; use search_text to locate relevant "
                                "content or read the file with a character-range capable tool"
                            )
                        has_more = True
                        next_start_line = line_number
                        truncated_reason = "output_limit"
                        break
                    if line_continues:
                        # The prefix happened to fit only because line numbers
                        # are short; the undisclosed remainder must never be
                        # reported as a successful complete line.
                        if not rendered_lines:
                            raise ValueError(
                                f"line {line_number} exceeds the read_file output limit of "
                                f"{MAX_OUTPUT_BYTES} bytes; use search_text to locate relevant "
                                "content or read the file with a character-range capable tool"
                            )
                        has_more = True
                        next_start_line = line_number
                        truncated_reason = "output_limit"
                        break

                    rendered_lines.append(rendered)
                    content_bytes += separator_bytes + rendered_bytes
        except UnicodeDecodeError as error:
            raise ValueError(f"file is not valid UTF-8 text: {path}") from error

        lines_returned = len(rendered_lines)
        end_line = start_line + lines_returned - 1 if lines_returned else None
        total_lines = None if has_more else last_seen_line
        return {
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "lines_returned": lines_returned,
            "has_more": has_more,
            "next_start_line": next_start_line,
            "total_lines": total_lines,
            "truncated_reason": truncated_reason,
            "content": "\n".join(rendered_lines),
        }

    def list_directory(path: str = ".", depth: int = 1) -> str:
        """List files and directories within the workspace."""
        if not 1 <= depth <= 4:
            raise ValueError("depth must be between 1 and 4")
        resolved = workspace.resolve(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"directory not found: {path}")
        rows: list[str] = []
        base_parts = len(resolved.parts)
        for entry in sorted(resolved.rglob("*")):
            relative_depth = len(entry.parts) - base_parts
            if relative_depth > depth:
                continue
            safe_entry = workspace.resolve(entry)
            suffix = "/" if safe_entry.is_dir() else ""
            rows.append(f"{safe_entry.relative_to(workspace.root).as_posix()}{suffix}")
        return _truncate("\n".join(rows) if rows else "[empty directory]")

    def search_text(query: str, path: str = ".", glob: str = "**/*", max_results: int = 100) -> str:
        """Search workspace text files using a regular expression."""
        if max_results < 1:
            raise ValueError("max_results must be positive")
        pattern = re.compile(query)
        resolved = workspace.resolve(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"directory not found: {path}")
        matches: list[str] = []
        truncated = False
        for file_path in _iter_search_files(resolved, glob):
            try:
                safe_file = workspace.resolve(file_path)
            except WorkspaceViolation:
                # A tree-wide search should ignore an escaping symlink rather
                # than aborting all useful results. The target is never read.
                continue
            if not safe_file.is_file():
                continue
            try:
                lines = safe_file.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if pattern.search(line):
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append(f"{safe_file.relative_to(workspace.root).as_posix()}:{line_number}:{line}")
            if truncated:
                break
        if truncated:
            matches.append(f"[results truncated at {max_results} matches]")
        return _truncate("\n".join(matches) if matches else "[no matches]")

    read_specs = [
        ToolSpec(read_file, risk=Risk.READ),
        ToolSpec(list_directory, risk=Risk.READ),
        ToolSpec(search_text, risk=Risk.READ),
    ]
    return [*read_specs, *build_capability_specs(root, max_timeout=max_timeout)]
