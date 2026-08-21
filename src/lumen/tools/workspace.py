from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a tool path escapes its configured workspace."""


class StaleWorkspaceResource(ValueError):
    """The resource no longer matches the revision authorized for mutation."""


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path

    def __init__(self, root: str | Path) -> None:
        object.__setattr__(self, "root", Path(root).expanduser().resolve())

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceViolation(f"path escapes workspace {self.root}: {path}")
        return resolved

    def resolve_for_mutation(self, path: str | Path) -> Path:
        """Resolve a mutation target while refusing every existing symlink hop."""

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        lexical = Path(os.path.abspath(candidate))
        if not lexical.is_relative_to(self.root):
            raise WorkspaceViolation(f"path escapes workspace {self.root}: {path}")
        current = lexical
        while current != self.root:
            if current.is_symlink():
                raise WorkspaceViolation(f"mutation path contains a symbolic link: {path}")
            current = current.parent
        resolved = lexical.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceViolation(f"path escapes workspace {self.root}: {path}")
        return lexical

    def revision(self, path: str | Path) -> str:
        target = self.resolve_for_mutation(path)
        if not target.exists():
            return "missing"
        if not target.is_file():
            raise WorkspaceViolation(f"mutation target is not a file: {path}")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return f"sha256:{digest}"

    def atomic_write(self, path: str | Path, body: bytes, *, expected_revision: str) -> None:
        """Durably publish bytes only if the target still has ``expected_revision``.

        Creation uses an atomic no-replace hard-link publication. Replacement
        revalidates after the temp file is fsynced and immediately before
        ``os.replace``; platforms without compare-and-swap rename cannot close
        the final syscall-sized race, so callers still verify the result.
        """

        target = self.resolve_for_mutation(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.resolve_for_mutation(path)
        if not target.parent.is_dir():
            raise NotADirectoryError(f"parent is not a directory: {target.parent}")
        temporary = target.parent / f".{target.name}.tmp-{secrets.token_hex(8)}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("atomic write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            target = self.resolve_for_mutation(path)
            self._require_revision(target, expected_revision)
            if expected_revision == "missing":
                try:
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError as error:
                    raise StaleWorkspaceResource(
                        f"STALE_RESOURCE: expected missing but target now exists: {path}"
                    ) from error
                temporary.unlink()
            else:
                self._require_revision(target, expected_revision)
                os.replace(temporary, target)
            self._fsync_directory(target.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def atomic_remove(self, path: str | Path, *, expected_revision: str) -> None:
        target = self.resolve_for_mutation(path)
        self._require_revision(target, expected_revision)
        if target.exists():
            target.unlink()
            self._fsync_directory(target.parent)

    def _require_revision(self, target: Path, expected_revision: str) -> None:
        actual = self.revision(target)
        if actual != expected_revision:
            raise StaleWorkspaceResource(
                "STALE_RESOURCE: expected "
                f"{expected_revision}, found {actual} at {target.relative_to(self.root)}"
            )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["StaleWorkspaceResource", "Workspace", "WorkspaceViolation"]
