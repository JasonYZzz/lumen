"""Pure, replayable presentation intent derived from durable tool data."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict


class ToolFamily(StrEnum):
    READ = "read"
    LIST = "list"
    SEARCH = "search"
    WEB = "web"
    MCP = "mcp"
    COMMAND = "command"
    EDIT = "edit"
    PLAN = "plan"
    SKILL = "skill"
    OTHER = "other"


class ToolCallView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tool_name: str
    family: ToolFamily
    title: str
    active_verb: str
    completed_verb: str
    detail: str | None = None
    groupable: bool = False
    group_key: str | None = None
    singular: str = "call"
    plural: str = "calls"
    tone: str = "tool"


class ToolResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tool_name: str
    status: Literal["success", "error"]
    title: str
    summary: str
    preview: str
    full_text: str
    tone: str = "tool"
    truncated: bool = False


CallPresenter = Callable[[dict[str, Any]], ToolCallView | Mapping[str, Any]]
ResultPresenter = Callable[[dict[str, Any], str, bool], ToolResultView | Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolPresentationSpec:
    call: CallPresenter | None = None
    result: ResultPresenter | None = None


@dataclass(frozen=True, slots=True)
class ToolPresentationDiagnostic:
    tool_name: str
    phase: Literal["call", "result"]
    error_type: str
    message: str


class ToolPresentationCatalog:
    """Derive presentation intent without affecting authoritative outcomes."""

    def __init__(self, specs: Mapping[str, object] | None = None, *, diagnostic_limit: int = 32) -> None:
        if diagnostic_limit < 1:
            raise ValueError("diagnostic_limit must be positive")
        self._specs = dict(specs or {})
        self._diagnostic_limit = diagnostic_limit
        self._diagnostics: list[ToolPresentationDiagnostic] = []

    @property
    def diagnostics(self) -> tuple[ToolPresentationDiagnostic, ...]:
        return tuple(self._diagnostics)

    def call_view(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        origin: str = "configured tool",
        risk: str = "external",
    ) -> ToolCallView:
        arguments = _safe_args(args)
        presenter = self._presentation(name).call
        if presenter is not None:
            try:
                return ToolCallView.model_validate(presenter(arguments))
            except Exception as error:
                self._record(name, "call", error)
        return _fallback_call(name, arguments, origin=origin, risk=risk)

    def result_view(
        self,
        name: str,
        args: Mapping[str, Any],
        result: str,
        *,
        is_error: bool,
    ) -> ToolResultView:
        arguments = _safe_args(args)
        presenter = self._presentation(name).result
        if presenter is not None:
            try:
                return ToolResultView.model_validate(presenter(arguments, result, is_error))
            except Exception as error:
                self._record(name, "result", error)
        return _fallback_result(name, result, is_error=is_error)

    def _presentation(self, name: str) -> ToolPresentationSpec:
        candidate = getattr(self._specs.get(name), "presentation", None)
        return candidate if isinstance(candidate, ToolPresentationSpec) else ToolPresentationSpec()

    def _record(self, name: str, phase: Literal["call", "result"], error: Exception) -> None:
        if len(self._diagnostics) >= self._diagnostic_limit:
            return
        self._diagnostics.append(
            ToolPresentationDiagnostic(name, phase, type(error).__name__, str(error)[:500])
        )


def _safe_args(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()}


def _fallback_call(
    name: str,
    args: dict[str, Any],
    *,
    origin: str,
    risk: str,
) -> ToolCallView:
    path = str(args.get("path", "")).strip() or None
    lowered = name.casefold()
    values: dict[str, Any]
    if _is_web_tool(lowered, origin):
        values = _call_values(
            ToolFamily.WEB,
            "正在搜索网络",
            "已搜索网络",
            _compact_query(str(args.get("query", ""))),
            groupable=True,
            group_key="web",
            singular="次搜索",
            plural="次搜索",
        )
    elif origin.startswith("mcp:"):
        service_key = origin.split(":", 1)[1].strip() or "external tool"
        service = _service_label(service_key)
        values = _call_values(
            ToolFamily.MCP,
            f"正在调用 {service}",
            f"已调用 {service}",
            _tool_label(name),
            groupable=risk == "read",
            group_key=f"mcp:{service_key.casefold()}",
        )
    elif name == "read_file":
        values = _call_values(
            ToolFamily.READ,
            "正在读取",
            "已读取",
            _workspace_label(path),
            groupable=True,
            group_key="local-inspection",
            singular="个文件",
            plural="个文件",
        )
    elif name == "list_directory":
        values = _call_values(
            ToolFamily.LIST,
            "正在查看目录",
            "已查看目录",
            _workspace_label(path),
            groupable=True,
            group_key="local-inspection",
            singular="个目录",
            plural="个目录",
        )
    elif name == "search_text":
        query = _compact_query(str(args.get("query", "")))
        target = _workspace_label(path)
        detail = f"“{query}” in {target}" if query else target
        values = _call_values(
            ToolFamily.SEARCH,
            "正在搜索",
            "已搜索",
            detail,
            groupable=True,
            group_key="local-search",
            singular="个模式",
            plural="个模式",
        )
    elif name in {"write_file", "edit_file"}:
        active, completed = (
            ("正在写入", "已写入") if name == "write_file" else ("正在编辑", "已编辑")
        )
        values = _call_values(ToolFamily.EDIT, active, completed, path, tone="mode-edit")
    elif name == "run_command":
        argv = args.get("argv")
        detail = (
            _one_line(" ".join(str(item) for item in cast(list[object], argv)), 72)
            if isinstance(argv, list)
            else None
        )
        values = _call_values(ToolFamily.COMMAND, "正在运行命令", "已运行命令", detail)
    elif name in {"load_skill", "read_skill_resource"}:
        detail = str(args.get("name", "")).strip() or None
        if name == "read_skill_resource":
            resource = str(args.get("path", "")).strip()
            detail = "/".join(item for item in (detail, resource) if item) or None
        active, completed = (
            ("正在加载 Skill", "已加载 Skill")
            if name == "load_skill"
            else ("正在读取 Skill 资源", "已读取 Skill 资源")
        )
        values = _call_values(ToolFamily.SKILL, active, completed, detail)
    elif name in {"set_plan", "update_step", "report_progress"}:
        labels = {
            "set_plan": ("正在规划", "已规划"),
            "update_step": ("正在更新任务", "已更新任务"),
            "report_progress": ("正在检查进度", "已检查进度"),
        }
        active, completed = labels[name]
        values = _call_values(ToolFamily.PLAN, active, completed, None, tone="mode-plan")
    else:
        values = _call_values(ToolFamily.OTHER, "正在使用工具", "已使用工具", _tool_label(name))
    return ToolCallView(tool_name=name, title=str(values["active_verb"]), **values)


def _call_values(
    family: ToolFamily,
    active_verb: str,
    completed_verb: str,
    detail: str | None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "family": family,
        "active_verb": active_verb,
        "completed_verb": completed_verb,
        "detail": detail,
        **extra,
    }


def _fallback_result(name: str, result: str, *, is_error: bool) -> ToolResultView:
    limit = 16_000
    full_text = result if len(result) <= limit else result[: limit - 1].rstrip() + "…"
    compact = " ".join(result.split())
    preview = compact if len(compact) <= 240 else compact[:239].rstrip() + "…"
    status: Literal["success", "error"] = "error" if is_error else "success"
    status_label = "失败" if is_error else "成功"
    return ToolResultView(
        tool_name=name,
        status=status,
        title=f"{_tool_label(name)} {status_label}",
        summary=preview or status_label,
        preview=preview,
        full_text=full_text,
        tone="error" if is_error else "tool",
        truncated=len(result) > limit,
    )


def _is_web_tool(name: str, origin: str) -> bool:
    return origin.casefold() in {"mcp:web", "mcp:browser"} or any(
        token in name for token in ("web_search", "search_web", "web_fetch", "fetch_url")
    )


def _service_label(value: str) -> str:
    known = {"github": "GitHub", "gitlab": "GitLab", "slack": "Slack"}
    return known.get(value.casefold(), value.replace("_", " ").replace("-", " ").title())


def _tool_label(value: str) -> str:
    return value.replace("_", " ").strip()


def _workspace_label(path: str | None) -> str:
    return "工作区" if not path or path == "." else _one_line(path, 54)


def _compact_query(value: str) -> str:
    clean = " ".join(value.split())
    if not clean:
        return ""
    alternatives = [part.strip() for part in clean.split("|") if part.strip()]
    if len(alternatives) > 1:
        visible = " | ".join(alternatives[:3])
        return f"{visible}…" if len(alternatives) > 3 else visible
    return _one_line(clean, 42)


def _one_line(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


__all__ = [
    "CallPresenter",
    "ResultPresenter",
    "ToolCallView",
    "ToolFamily",
    "ToolPresentationCatalog",
    "ToolPresentationDiagnostic",
    "ToolPresentationSpec",
    "ToolResultView",
]
