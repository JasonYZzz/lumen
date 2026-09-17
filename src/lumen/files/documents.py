from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from lumen.tools.workspace import Workspace

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def read_workspace_document(root: Path, path: str) -> bytes:
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
            if info.st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("Document exceeds the 20 MiB preview/download limit")
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(content) > MAX_DOCUMENT_BYTES:
                raise ValueError("Document exceeds the 20 MiB preview/download limit")
            return content
    finally:
        os.close(descriptor)
