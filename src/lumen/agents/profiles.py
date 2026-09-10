"""Discover builtin, user, and trusted project Agent profiles."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import yaml

from lumen.reasoning import ReasoningLevel

from .types import AgentProfile, WorkspaceMode

_BUILTIN_PROFILES = {
    "default": AgentProfile(
        name="default",
        description="在隔离 worktree 中执行通用委派任务。",
        workspace_mode=WorkspaceMode.WORKTREE,
        instructions=(
            "只完成被委派的任务。严格控制改动范围, 验证结果, "
            "并向父 Agent 返回简洁且有证据支持的摘要。"
        ),
        revision="builtin:default:v1",
    ),
    "explorer": AgentProfile(
        name="explorer",
        description="只读调查与证据收集。",
        workspace_mode=WorkspaceMode.READ_ONLY,
        instructions=(
            "只使用观察类工具调查被委派的问题。引用具体路径或结果, 不得声称修改了状态。"
        ),
        revision="builtin:explorer:v1",
    ),
    "worker": AgentProfile(
        name="worker",
        description="在隔离 worktree 中完成聚焦的实现与验证。",
        workspace_mode=WorkspaceMode.WORKTREE,
        instructions=(
            "在隔离 worktree 中实现范围明确的委派修改。运行相关验证, "
            "并概述准确改动、测试和剩余风险。"
        ),
        revision="builtin:worker:v1",
    ),
}


class AgentProfileLoader:
    """Load profiles with project > user > builtin precedence."""

    def __init__(self, workspace: str | Path, *, include_project: bool) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.include_project = include_project
        self.warnings: list[str] = []

    def discover(self) -> dict[str, AgentProfile]:
        profiles = dict(_BUILTIN_PROFILES)
        for profile in self._scan(Path.home() / ".lumen" / "agents", "user"):
            profiles[profile.name] = profile
        if self.include_project:
            for profile in self._scan(self.workspace / ".lumen" / "agents", "project"):
                profiles[profile.name] = profile
        return profiles

    def _scan(self, root: Path, source: str) -> list[AgentProfile]:
        if not root.is_dir():
            return []
        profiles: list[AgentProfile] = []
        for path in sorted(root.glob("*.md")):
            try:
                profiles.append(self._load(path, source))
            except (OSError, ValueError, yaml.YAMLError) as error:
                self.warnings.append(f"ignored agent profile {path}: {error}")
        return profiles

    @staticmethod
    def _load(path: Path, source: str) -> AgentProfile:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = _parse_frontmatter(text)
        name = str(frontmatter.get("name") or path.stem)
        description = str(frontmatter.get("description") or "").strip()
        if not description:
            raise ValueError("description is required")
        instructions = body.strip()
        if not instructions:
            raise ValueError("instruction body is required")
        raw_tools = frontmatter.get("tools")
        tools: tuple[str, ...] | None
        if raw_tools is None:
            tools = None
        elif isinstance(raw_tools, list):
            tool_items = cast(list[object], raw_tools)
            tools = tuple(str(item).strip() for item in tool_items if str(item).strip())
        elif isinstance(raw_tools, str):
            tools = tuple(item.strip() for item in raw_tools.split(",") if item.strip())
        else:
            raise ValueError("tools must be a list or comma-separated string")
        revision = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return AgentProfile(
            name=name,
            description=description,
            instructions=instructions,
            workspace_mode=WorkspaceMode(str(frontmatter.get("workspace-mode", "read-only"))),
            tools=tools,
            model=(str(frontmatter["model"]) if frontmatter.get("model") else None),
            reasoning_effort=(
                ReasoningLevel(str(frontmatter["reasoning-effort"]))
                if frontmatter.get("reasoning-effort")
                else None
            ),
            source=source,
            file_path=str(path.resolve()),
            revision=revision,
        )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("unterminated YAML frontmatter")
    loaded: object = yaml.safe_load(text[4:end]) or {}
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter must be a mapping")
    return cast(dict[str, Any], loaded), text[end + 5 :]


__all__ = ["AgentProfileLoader"]
