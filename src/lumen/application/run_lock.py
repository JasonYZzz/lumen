"""Process-local facade over one workspace-scoped OS advisory execution lock."""

from __future__ import annotations

import errno
import os
from pathlib import Path

from lumen.tools.workspace import Workspace

if os.name == "nt":  # pragma: win32 cover
    import msvcrt

    def _acquire_descriptor(descriptor: int) -> None:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

    def _release_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:  # pragma: posix cover
    import fcntl

    def _acquire_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class WorkspaceRunLock:
    """Prevent two local Hosts from executing against one workspace at once.

    The lock owns no run state. The OS releases it when the descriptor closes
    or the process exits, while other processes remain free to read the
    workspace and Session journal.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = Workspace(workspace)
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> bool:
        if self._descriptor is not None:
            return True
        lock_path = self._workspace.resolve_for_mutation(".lumen/run.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._workspace.resolve_for_mutation(".lumen/run.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _acquire_descriptor(descriptor)
        except OSError as error:
            os.close(descriptor)
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            _release_descriptor(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["WorkspaceRunLock"]
