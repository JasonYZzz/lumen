from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a tool path escapes its configured workspace."""


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
