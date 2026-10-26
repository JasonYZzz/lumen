from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lumen.tools.workspace import Workspace

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def read_workspace_document(root: Path, path: str, *, modified_since: float | None = None) -> bytes:
    """Bounded user-initiated document read; never follow symlinks or special files."""
    relative = Path(path)
    if not path or relative.is_absolute() or any(part.startswith(".") for part in relative.parts):
        raise ValueError("Document path must be workspace-relative without hidden or parent segments")
    # Windows lacks descriptor-relative, no-follow opens. Do not weaken this
    # security boundary to a path check followed by a race-prone ordinary open.
    if sys.platform == "win32":
        raise ValueError("Secure document preview/download is supported on macOS and Linux")
    workspace = Workspace(root)
    workspace.resolve_for_mutation(relative)
    descriptor = os.open(workspace.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(file_descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Document must be a regular file")
            if modified_since is not None and info.st_mtime < modified_since:
                raise ValueError("Document was not produced during this run")
            if info.st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("Document exceeds the 20 MiB preview/download limit")
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(content) > MAX_DOCUMENT_BYTES:
                raise ValueError("Document exceeds the 20 MiB preview/download limit")
            return content
    finally:
        os.close(descriptor)


def linked_document_paths(text: str, root: Path) -> tuple[str, ...]:
    """Bounded explicit local attachments, never a workspace scan or proof of existence."""
    paths: list[str] = []
    for match in re.finditer(r"!?\[[^\]\n]{0,500}\]\(", text):
        candidate = text[match.end():match.end() + 2050]
        depth = 1
        destination = ""
        escaped = False
        for index, character in enumerate(candidate):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    destination = candidate[:index]
                    break
            elif character == "\n":
                break
        if destination.startswith("<") and destination.endswith(">"):
            destination = destination[1:-1]
        elif any(character.isspace() for character in destination):
            continue
        if not destination or len(destination) > 2048:
            continue
        try:
            value = urlsplit(re.sub(r"\\([\\()])", r"\1", destination))
        except ValueError:
            continue
        if value.scheme or value.netloc:
            continue
        path = Path(unquote(value.path))
        if path.is_absolute():
            try:
                path = path.relative_to(root)
            except ValueError:
                continue
        if not path.suffix or any(part.startswith(".") for part in path.parts):
            continue
        normalized = path.as_posix()
        if normalized not in paths:
            paths.append(normalized)
        if len(paths) == 32:
            break
    return tuple(paths)
