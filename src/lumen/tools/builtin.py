from __future__ import annotations

import re
from pathlib import Path

from lumen.tools.capability import build_capability_specs
from lumen.tools.spec import Risk, ToolSpec
from lumen.tools.workspace import Workspace

MAX_OUTPUT_BYTES = 64 * 1024


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    kept = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}\n[output truncated at {MAX_OUTPUT_BYTES} bytes]"


def build_builtin_specs(root: str | Path, *, max_timeout: float = 60.0) -> list[ToolSpec]:
    """Return the read-only builtins followed by the capability tools.

    Read tools are unconditional; the capability tools (write/edit/run) join the
    registry and go through the standard permission path so the model can only
    execute them after approval by default.
    """

    workspace = Workspace(root)

    def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
        """Read a UTF-8 text file inside the workspace with line numbers."""
        if start_line < 1 or max_lines < 1:
            raise ValueError("start_line and max_lines must be positive")
        resolved = workspace.resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return _truncate("\n".join(f"{number}: {line}" for number, line in enumerate(selected, start_line)))

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
        for file_path in sorted(resolved.glob(glob)):
            if not file_path.is_file():
                continue
            safe_file = workspace.resolve(file_path)
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
