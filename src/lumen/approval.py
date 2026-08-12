"""Session approval policy and user-facing approval payloads."""

from __future__ import annotations

import difflib
import json
import os
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, cast

from lumen.events import ApprovalRequest


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    ACCEPT_EDITS = "accept_edits"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str) -> ApprovalMode:
        return cls(value)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool = False
    requires_confirmation: bool = True
    source: str = "user"
    message: str = "approval required"


class ApprovalPolicy:
    """Resolve session-mode decisions at the single approval seam."""

    _AUTO_RISKS = frozenset({"read", "write", "execute", "external"})
    _EDIT_TOOLS = frozenset({"write_file", "edit_file"})
    _READ_ONLY_COMMANDS = frozenset(
        {
            "cat",
            "du",
            "fd",
            "file",
            "find",
            "grep",
            "head",
            "ls",
            "pwd",
            "rg",
            "stat",
            "tail",
            "tree",
            "wc",
            "which",
            "whereis",
        }
    )
    _READ_ONLY_GIT_COMMANDS = frozenset(
        {"blame", "describe", "diff", "grep", "log", "ls-files", "rev-parse", "shortlog", "show", "status"}
    )
    _FIND_MUTATING_FLAGS = frozenset(
        {"-delete", "-exec", "-execdir", "-fprint", "-fprint0", "-ok", "-okdir"}
    )
    _COMMON_FILESYSTEM_COMMANDS = frozenset({"cp", "mkdir", "mv", "touch"})
    _PROTECTED_ROOTS = frozenset({".claude", ".git", ".lumen"})

    def decide(self, request: ApprovalRequest, mode: ApprovalMode | str) -> ApprovalDecision:
        parsed = mode if isinstance(mode, ApprovalMode) else ApprovalMode.parse(mode)
        # Deprecated writable child calls preserve their historical explicit
        # spawn approval. Native ``spawn_agent`` worktree creation is isolated
        # and instead requires approval at import time.
        if request.name == "spawn_child" and request.args.get("kind") == "worktree":
            return ApprovalDecision(
                message="writable child spawns always require explicit approval"
            )
        if request.risk == "read":
            return self._auto_decision(parsed, request)
        if request.risk == "external_unknown" or self._targets_protected_path(request):
            return ApprovalDecision()
        if parsed is ApprovalMode.MANUAL:
            return ApprovalDecision()
        if parsed is ApprovalMode.ACCEPT_EDITS:
            if request.origin == "builtin" and request.name in self._EDIT_TOOLS:
                return self._auto_decision(parsed, request)
            if self._is_workspace_filesystem_command(request):
                return self._auto_decision(parsed, request)
            return ApprovalDecision()
        if request.risk in self._AUTO_RISKS:
            return self._auto_decision(parsed, request)
        return ApprovalDecision()

    @classmethod
    def _is_read_only_command(cls, request: ApprovalRequest) -> bool:
        if request.origin != "builtin" or request.name != "run_command":
            return False
        argv = request.args.get("argv")
        if not isinstance(argv, list) or not argv:
            return False
        items = cast(list[object], argv)
        command = os.path.basename(str(items[0]))
        args = [str(item) for item in items[1:]]
        if command == "git":
            return bool(args) and args[0] in cls._READ_ONLY_GIT_COMMANDS
        if command == "find":
            return not any(item in cls._FIND_MUTATING_FLAGS for item in args)
        return command in cls._READ_ONLY_COMMANDS

    @classmethod
    def is_read_only(cls, request: ApprovalRequest) -> bool:
        return request.risk == "read" or cls._is_read_only_command(request)

    @classmethod
    def _is_workspace_filesystem_command(cls, request: ApprovalRequest) -> bool:
        """Recognize Claude-style common file commands without path escape.

        ``run_command`` never invokes a shell, so rejecting absolute paths and
        ``..`` components is sufficient to keep these convenience commands
        under their already workspace-bounded cwd.
        """

        if request.origin != "builtin" or request.name != "run_command":
            return False
        argv = request.args.get("argv")
        if not isinstance(argv, list) or not argv:
            return False
        items = cast(list[object], argv)
        command = os.path.basename(str(items[0]))
        if command not in cls._COMMON_FILESYSTEM_COMMANDS:
            return False
        path_args = [str(item) for item in items[1:] if not str(item).startswith("-")]
        if not path_args:
            return False
        return all(
            not PurePosixPath(item).is_absolute() and ".." not in PurePosixPath(item).parts
            for item in path_args
        )

    @classmethod
    def _targets_protected_path(cls, request: ApprovalRequest) -> bool:
        if request.origin != "builtin" or request.name not in cls._EDIT_TOOLS:
            return False
        raw_path = request.args.get("path")
        if not isinstance(raw_path, str):
            return False
        parts = PurePosixPath(raw_path.replace("\\", "/")).parts
        return any(part in cls._PROTECTED_ROOTS for part in parts)

    @staticmethod
    def _auto_decision(mode: ApprovalMode, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            approved=True,
            requires_confirmation=False,
            source="policy",
            message=f"auto-approved (mode={mode.value}, risk={request.risk})",
        )


@dataclass(frozen=True, slots=True)
class ApprovalViewModel:
    title: str
    preview: str
    full_text: str


class ApprovalPresenter:
    """Turn tool-specific arguments into a complete, reviewable payload."""

    #: Cap on existing-file content read for write_file overwrite diffs, so a
    #: huge target cannot blow up the approval payload.
    _MAX_READ_CHARS = 100_000

    def __init__(self, *, preview_chars: int = 4_000, workspace_root: str | Path | None = None) -> None:
        self.preview_chars = preview_chars
        # Root against which write_file paths resolve; used to read the old
        # content for an overwrite diff. ``None`` keeps the raw content dump.
        self.workspace_root = Path(workspace_root) if workspace_root is not None else None

    def build(self, request: ApprovalRequest) -> ApprovalViewModel:
        rendered = self._render(request.name, request.args)
        preview = rendered[: self.preview_chars]
        if len(rendered) > self.preview_chars:
            preview += "\n… (expand for complete arguments)"
        return ApprovalViewModel(
            title=f"{request.name} · {request.risk} · {request.origin}",
            preview=preview,
            full_text=rendered,
        )

    def _render(self, name: str, args: dict[str, Any]) -> str:
        if name == "run_command":
            argv = args.get("argv")
            command = (
                " ".join(shlex.quote(str(part)) for part in cast(list[object], argv))
                if isinstance(argv, list)
                else ""
            )
            lines = [f"$ {command}" if command else "$ <missing argv>"]
            if args.get("cwd") not in (None, ""):
                lines.append(f"cwd: {args['cwd']}")
            return "\n".join(lines)
        if name == "edit_file":
            path = str(args.get("path", "<missing path>"))
            old = str(args.get("old_text", args.get("find", "")))
            new = str(args.get("new_text", args.get("replace", "")))
            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"{path} (before)",
                    tofile=f"{path} (after)",
                )
            )
            return diff or f"path: {path}\n(no textual change)"
        if name == "write_file":
            path = str(args.get("path", "<missing path>"))
            content = str(args.get("content", ""))
            size = len(content.encode("utf-8"))
            header = f"path: {path}\nsize: {size} bytes\n\n"
            # Render a before -> after diff. Overwrites read the file that is
            # about to be replaced (approval happens pre-execution, so it is
            # still on disk); a missing/unreadable target falls back to the
            # raw content dump, and a new file diffs against "" so the preview
            # reads as an all-additions view.
            old: str | None = "" if not args.get("overwrite") else self._read_existing(path)
            if old is None:
                return header + content
            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"{path} (before)",
                    tofile=f"{path} (after)",
                )
            )
            return header + (diff or "(no textual change)")
        return json.dumps(args, ensure_ascii=False, indent=2, default=str)

    def _read_existing(self, path: str) -> str | None:
        """Best-effort read of the file a write_file overwrite would replace.

        Returns ``None`` when no workspace root is known, the path escapes the
        workspace, or the file cannot be decoded as UTF-8 - the caller then
        falls back to the raw content dump. A missing file yields ``""``,
        which renders as an all-additions diff.
        """

        if self.workspace_root is None:
            return None
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        candidate = self.workspace_root / relative
        if not candidate.is_file():
            return ""
        try:
            return candidate.read_text(encoding="utf-8")[: self._MAX_READ_CHARS]
        except (OSError, UnicodeDecodeError):
            return None


__all__ = [
    "ApprovalDecision",
    "ApprovalMode",
    "ApprovalPolicy",
    "ApprovalPresenter",
    "ApprovalViewModel",
]
