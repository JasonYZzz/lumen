from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

from lumen.tools.workspace import StaleWorkspaceResource, Workspace


def web_session_secret(workspace: Path, endpoint: str) -> str:
    """Keep browser authentication across CLI restarts, isolated per workspace and endpoint."""
    files = Workspace(workspace)
    identity = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
    path = files.resolve_for_mutation(f".lumen/web-auth-{identity}.key")
    if not path.exists():
        try:
            files.atomic_write(path, secrets.token_urlsafe(32).encode(), expected_revision="missing")
        except StaleWorkspaceResource:
            pass  # A concurrent launcher published the key first.
    path = files.resolve_for_mutation(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.name == "posix":
        root = os.open(files.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open(".lumen", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                descriptor = os.open(path.name, flags, dir_fd=directory)
            finally:
                os.close(directory)
        finally:
            os.close(root)
    else:
        descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 43:
            raise ValueError("invalid Web authentication key file")
        if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ValueError("Web authentication key must be owned by the current user with mode 0600")
        secret = os.read(descriptor, 44).decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
            raise ValueError("invalid Web authentication key file")
        return secret
    finally:
        os.close(descriptor)
