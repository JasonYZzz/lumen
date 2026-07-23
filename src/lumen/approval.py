"""Session approval policy and user-facing approval payloads."""

from __future__ import annotations

import difflib
import json
import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from lumen.events import ApprovalRequest


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    ACCEPT_EDITS = "accept_edits"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str) -> ApprovalMode:
        return cls.MANUAL if value == "ask" else cls(value)


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

    def decide(self, request: ApprovalRequest, mode: ApprovalMode | str) -> ApprovalDecision:
        parsed = mode if isinstance(mode, ApprovalMode) else ApprovalMode.parse(mode)
        if request.risk == "external_unknown" or parsed is ApprovalMode.MANUAL:
            return ApprovalDecision()
        if parsed is ApprovalMode.ACCEPT_EDITS:
            if request.origin == "builtin" and request.name in self._EDIT_TOOLS:
                return self._auto_decision(parsed, request)
            return ApprovalDecision()
        if request.risk in self._AUTO_RISKS:
            return self._auto_decision(parsed, request)
        return ApprovalDecision()

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

    def __init__(self, *, preview_chars: int = 4_000) -> None:
        self.preview_chars = preview_chars

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
            return f"path: {path}\nsize: {size} bytes\n\n{content}"
        return json.dumps(args, ensure_ascii=False, indent=2, default=str)


__all__ = [
    "ApprovalDecision",
    "ApprovalMode",
    "ApprovalPolicy",
    "ApprovalPresenter",
    "ApprovalViewModel",
]
