# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import codecs
import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, TypeAlias

from pydantic import Field

from lumen.config import SandboxConfig
from lumen.constants import IGNORED_DIRS
from lumen.tools.capability import build_capability_specs
from lumen.tools.git import build_git_specs
from lumen.tools.spec import Risk, ToolConcurrency, ToolSpec
from lumen.tools.workspace import Workspace, WorkspaceViolation

if TYPE_CHECKING:
    from lumen.work_products import TaskWorkspace

MAX_OUTPUT_BYTES = 64 * 1024
_BINARY_SAMPLE_BYTES = 8 * 1024

ReadFileResult: TypeAlias = dict[str, str | int | bool | None]


def _truncate(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    kept = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}\n[输出已在 {MAX_OUTPUT_BYTES} 字节处截断]"


def _iter_search_files(base: Path, glob: str) -> Iterator[Path]:
    """Yield in deterministic directory order, pruning before descent."""
    pattern = tuple(Path(glob).parts)
    if Path(glob).is_absolute() or ".." in pattern:
        raise ValueError("glob 必须是工作区内的相对模式")
    if glob.endswith("/"):
        return
    for current, directory_names, file_names in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_DIRS and not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            candidate = current_path / name
            if _glob_matches(candidate.relative_to(base).parts, pattern):
                yield candidate


def _glob_matches(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_glob_matches(parts[index:], pattern[1:]) for index in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _glob_matches(parts[1:], pattern[1:])


def build_builtin_specs(
    root: str | Path,
    *,
    max_timeout: float = 60.0,
    sandbox_config: SandboxConfig | None = None,
    task_workspace: TaskWorkspace | None = None,
    workspace: Workspace | None = None,
) -> list[ToolSpec]:
    """Return the read-only builtins followed by the capability tools.

    Read tools are unconditional; the capability tools (write/edit/run) join the
    registry and go through the standard permission path so the model can only
    execute them after approval by default.
    """

    if workspace is None:
        workspace = task_workspace.workspace if task_workspace is not None else Workspace(
            root, allow_external_paths=sandbox_config is not None and sandbox_config.mode == "disabled",
            use_session_permissions=True,
        )
    path_scope = (
        "路径相对工作区解析；实际访问范围见当前运行上下文。完全访问时可用绝对路径或 ~ 访问其他本地目录。"
        if workspace.use_session_permissions else "路径必须位于工作区内，不能通过父会话授权越界。"
    )

    def read_file(
        path: Annotated[str, Field(description="UTF-8 文件路径；可访问范围见工具说明。")],
        start_line: Annotated[int, Field(description="从 1 开始的起始行号。", ge=1)] = 1,
        max_lines: Annotated[int, Field(description="本次最多返回的行数。", ge=1)] = 400,
    ) -> ReadFileResult:
        """分页读取一个有权限访问的 UTF-8 文本文件，并返回续读元数据。

        结果包含 content、start_line、end_line、lines_returned、has_more、
        next_start_line、total_lines（到达文件末尾时）和 truncated_reason。
        若 has_more 为 true，请把 next_start_line 作为下一次 start_line 继续读取。
        """
        if start_line < 1 or max_lines < 1:
            raise ValueError("start_line 和 max_lines 必须是正整数")
        resolved = workspace.resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"文件不存在: {path}")

        # Reject obvious binary data before creating a text decoder. This is a
        # bounded probe rather than a full-file pre-read, so a small page never
        # causes a large file to be loaded into memory.
        with resolved.open("rb") as binary_file:
            sample = binary_file.read(_BINARY_SAMPLE_BYTES)
        if b"\x00" in sample:
            raise ValueError(f"read_file 不支持二进制文件: {path}")
        try:
            # ``final=False`` permits the bounded sample to end in the middle
            # of an otherwise valid multi-byte code point.
            codecs.getincrementaldecoder("utf-8")(errors="strict").decode(sample, final=False)
        except UnicodeDecodeError as error:
            raise ValueError(f"文件不是有效的 UTF-8 文本: {path}") from error

        rendered_lines: list[str] = []
        content_bytes = 0
        last_seen_line = 0
        has_more = False
        next_start_line: int | None = None
        truncated_reason: str | None = None

        try:
            with resolved.open("r", encoding="utf-8", errors="strict", newline=None) as text_file:
                line_number = 0
                while True:
                    if len(rendered_lines) >= max_lines:
                        if text_file.read(1):
                            has_more = True
                            next_start_line = line_number + 1
                            truncated_reason = "line_limit"
                        break

                    # Bound the allocation for one pathological line. TextIO's
                    # size is in characters, so the retained object is at most
                    # roughly 4x the byte budget for valid UTF-8 instead of an
                    # arbitrarily large minified line.
                    raw_line = text_file.readline(MAX_OUTPUT_BYTES + 1)
                    if raw_line == "":
                        break
                    line_number += 1
                    last_seen_line = line_number
                    if line_number < start_line:
                        # Drain the remainder of an overlong skipped line in
                        # bounded chunks without retaining it.
                        while len(raw_line) == MAX_OUTPUT_BYTES + 1 and not raw_line.endswith("\n"):
                            raw_line = text_file.readline(MAX_OUTPUT_BYTES + 1)
                        continue

                    line = raw_line.removesuffix("\n")
                    rendered = f"{line_number}: {line}"
                    separator_bytes = 1 if rendered_lines else 0
                    rendered_bytes = len(rendered.encode("utf-8"))
                    line_continues = len(raw_line) == MAX_OUTPUT_BYTES + 1 and not raw_line.endswith("\n")
                    if content_bytes + separator_bytes + rendered_bytes > MAX_OUTPUT_BYTES:
                        if not rendered_lines:
                            raise ValueError(
                                f"第 {line_number} 行超过 read_file 的 {MAX_OUTPUT_BYTES} 字节输出上限；"
                                "请用 search_text 定位相关内容，或使用支持字符范围读取的工具"
                            )
                        has_more = True
                        next_start_line = line_number
                        truncated_reason = "output_limit"
                        break
                    if line_continues:
                        # The prefix happened to fit only because line numbers
                        # are short; the undisclosed remainder must never be
                        # reported as a successful complete line.
                        if not rendered_lines:
                            raise ValueError(
                                f"第 {line_number} 行超过 read_file 的 {MAX_OUTPUT_BYTES} 字节输出上限；"
                                "请用 search_text 定位相关内容，或使用支持字符范围读取的工具"
                            )
                        has_more = True
                        next_start_line = line_number
                        truncated_reason = "output_limit"
                        break

                    rendered_lines.append(rendered)
                    content_bytes += separator_bytes + rendered_bytes
        except UnicodeDecodeError as error:
            raise ValueError(f"文件不是有效的 UTF-8 文本: {path}") from error

        lines_returned = len(rendered_lines)
        end_line = start_line + lines_returned - 1 if lines_returned else None
        total_lines = None if has_more else last_seen_line
        return {
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "lines_returned": lines_returned,
            "has_more": has_more,
            "next_start_line": next_start_line,
            "total_lines": total_lines,
            "truncated_reason": truncated_reason,
            "content": "\n".join(rendered_lines),
        }

    def list_directory(
        path: Annotated[str, Field(description="目录路径；可访问范围见工具说明。")] = ".",
        depth: Annotated[int, Field(description="列出的目录层级，范围为 1 到 4。", ge=1, le=4)] = 1,
    ) -> str:
        """按目录顺序列出有权限访问的目录；depth 限制遍历，不深入默认忽略目录。"""
        if not 1 <= depth <= 4:
            raise ValueError("depth 必须在 1 到 4 之间")
        resolved = workspace.resolve(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"目录不存在: {path}")
        rows: list[str] = []
        output_bytes = 0
        for current, directories, files in os.walk(resolved, topdown=True, followlinks=False):
            parent = Path(current)
            level = len(parent.relative_to(resolved).parts) + 1
            entries = sorted([*directories, *files])
            directories[:] = sorted(
                name for name in directories
                if level < depth and name not in IGNORED_DIRS and not (parent / name).is_symlink()
            )
            for name in entries:
                safe_entry = workspace.resolve(parent / name)
                suffix = "/" if safe_entry.is_dir() else ""
                row = f"{workspace.resource_path(safe_entry)}{suffix}"
                rows.append(row)
                output_bytes += len(row.encode("utf-8")) + 1
                if output_bytes > MAX_OUTPUT_BYTES:
                    return _truncate("\n".join(rows))
        return _truncate("\n".join(rows) if rows else "[空目录]")

    def search_text(
        query: Annotated[
            str,
            Field(
                description=(
                    "用于匹配每一行的 Python 正则表达式。空格会按字面匹配; 多个备选项应写成"
                    " (run|_run|step), 不要在 | 两侧添加无意的空格。"
                )
            ),
        ],
        path: Annotated[
            str,
            Field(description="现有文件或目录的路径；传入文件时只搜索该文件，可访问范围见工具说明。"),
        ] = ".",
        glob: Annotated[
            str,
            Field(description="path 为目录时使用的 glob；path 为文件时忽略。"),
        ] = "**/*",
        max_results: Annotated[int, Field(description="本次最多返回的匹配数。", ge=1)] = 100,
    ) -> str:
        """用正则表达式搜索有权限访问的文本；path 可以是一个文件或目录。"""
        if max_results < 1:
            raise ValueError("max_results 必须是正整数")
        pattern = re.compile(query)
        resolved = workspace.resolve(path)
        if resolved.is_file():
            candidates = (resolved,)
        elif resolved.is_dir():
            candidates = _iter_search_files(resolved, glob)
        else:
            raise FileNotFoundError(f"文件或目录不存在: {path}")
        matches: list[str] = []
        truncated = False
        output_bytes = 0
        for file_path in candidates:
            try:
                safe_file = workspace.resolve(file_path)
            except WorkspaceViolation:
                # A tree-wide search should ignore an escaping symlink rather
                # than aborting all useful results. The target is never read.
                continue
            if not safe_file.is_file():
                continue
            try:
                with safe_file.open(encoding="utf-8") as handle:
                    line_number = 0
                    while line := handle.readline(MAX_OUTPUT_BYTES + 1):
                        line_number += 1
                        if len(line) > MAX_OUTPUT_BYTES and not line.endswith("\n"):
                            raise ValueError(
                                f"{workspace.resource_path(safe_file)}:{line_number} "
                                f"超过 search_text 的 {MAX_OUTPUT_BYTES} 字符行上限；请缩小读取范围"
                            )
                        text = line.rstrip("\r\n")
                        if not pattern.search(text):
                            continue
                        if len(matches) >= max_results:
                            truncated = True
                            break
                        row = (
                            f"{workspace.resource_path(safe_file)}:{line_number}:"
                            f"{text}"
                        )
                        matches.append(row)
                        output_bytes += len(row.encode("utf-8")) + 1
                        if output_bytes > MAX_OUTPUT_BYTES:
                            return _truncate("\n".join(matches))
            except (OSError, UnicodeDecodeError):
                continue
            if truncated:
                break
        if truncated:
            matches.append(f"[结果已在 {max_results} 条匹配处截断]")
        return _truncate("\n".join(matches) if matches else "[没有匹配结果]")

    read_specs = [
        ToolSpec(
            read_file,
            description=(read_file.__doc__ or "") + "\n" + path_scope,
            risk=Risk.READ,
            concurrency=lambda _args: ToolConcurrency.PARALLEL_SAFE,
        ),
        ToolSpec(
            list_directory,
            description=(list_directory.__doc__ or "") + "\n" + path_scope,
            risk=Risk.READ,
            concurrency=lambda _args: ToolConcurrency.PARALLEL_SAFE,
        ),
        ToolSpec(
            search_text,
            description=(search_text.__doc__ or "") + "\n" + path_scope,
            risk=Risk.READ,
            concurrency=lambda _args: ToolConcurrency.PARALLEL_SAFE,
        ),
    ]
    return [
        *read_specs,
        *build_capability_specs(
            root,
            max_timeout=max_timeout,
            sandbox_config=sandbox_config,
            task_workspace=task_workspace,
            workspace=workspace,
        ),
        *build_git_specs(
            root,
            max_timeout=max_timeout,
            sandbox_config=sandbox_config,
        ),
    ]
