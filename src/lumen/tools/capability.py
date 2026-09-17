"""Workspace-scoped write, exact edit, and argv command tools.

These are the workspace capability tools the model uses to actually act on the
project: writing files, editing files by exact match, and running commands
without going through a shell. All three are workspace-scoped and the model is
gated by the standard permission system before any of them can execute.
"""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lumen.config import SandboxConfig
from lumen.sandbox import PreparedSandboxCommand, SandboxRunner
from lumen.tools.spec import EffectKind, Risk, ToolSpec
from lumen.tools.workspace import Workspace

if TYPE_CHECKING:
    from lumen.work_products import TaskWorkspace

MAX_OUTPUT_BYTES = 64 * 1024


class BoundedCollector:
    """Accumulates a stream's bytes keeping a fixed head and tail only.

    Reading a child pipe with ``communicate()`` buffers the entire output in
    memory — a runaway command writing 100 MB would OOM the run. This collector
    is fed incrementally: it keeps the first ``cap`` bytes verbatim, the last
    ``cap`` bytes in a rolling tail buffer, and discards everything in between
    while still counting total bytes seen. The final rendering joins head and
    tail with a truncation marker so both the start and end of a long log stay
    visible (the end usually carries the real signal: the error/stack trace).
    """

    def __init__(self, *, cap: int = MAX_OUTPUT_BYTES) -> None:
        self._cap = cap
        self._total = 0
        self._head = bytearray()
        self._tail = bytearray()
        self._truncated = False

    def feed(self, data: bytes) -> None:
        """Accept one chunk from the pipe, bounding memory usage."""
        self._total += len(data)
        if not self._truncated and len(self._head) < self._cap:
            remaining_head = self._cap - len(self._head)
            self._head.extend(data[:remaining_head])
            data = data[remaining_head:]
            if len(self._head) >= self._cap and data:
                self._truncated = True
        # Rolling tail: keep only the last ``cap`` bytes across all feeds.
        if data:
            self._truncated = True
            self._tail.extend(data)
            if len(self._tail) > self._cap:
                del self._tail[: len(self._tail) - self._cap]

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def truncated(self) -> bool:
        return self._truncated

    def render(self) -> str:
        """Return the bounded text: head [+ marker + tail] when truncated."""
        head = bytes(self._head).decode("utf-8", errors="replace")
        if not self._truncated:
            return head
        tail = bytes(self._tail).decode("utf-8", errors="replace")
        return (
            f"{head}\n"
            f"[... {self._total - len(self._head) - len(self._tail)} bytes truncated "
            f"(head {len(self._head)} + tail {len(self._tail)} kept) ...]\n"
            f"{tail}"
        )


def build_capability_specs(
    root: str | Path,
    *,
    max_timeout: float,
    sandbox_config: SandboxConfig | None = None,
    task_workspace: TaskWorkspace | None = None,
) -> list[ToolSpec]:
    """Build the workspace write/edit/exec specs.

    ``max_timeout`` is the upper bound the model cannot exceed with a per-call
    ``timeout`` argument; clamping happens inside ``run_command``.
    """

    workspace = Workspace(root)
    sandbox = SandboxRunner(
        workspace.root,
        sandbox_config or SandboxConfig(mode="disabled"),
    )

    def write_file(path: str, content: str, overwrite: bool = False) -> str:
        """Write UTF-8 text to ``path`` inside the workspace.

        Use ``outputs/<descriptive-name>`` for generated reports, exports, and
        other deliverables when the user did not provide an explicit path.
        Source-code changes should keep their actual project path.

        Creates missing parent directories. Refuses to overwrite an existing
        file unless ``overwrite=True``. The write is atomic: a sibling temp file
        is fsynced and then ``os.replace``-ed into place so a cancellation or
        crash cannot leave partially written content at the destination.
        """

        resolved = workspace.resolve_for_mutation(path)
        if resolved.exists() and not overwrite:
            raise FileExistsError(f"file already exists; pass overwrite=True to replace: {path}")
        expected_revision = workspace.revision(resolved)
        encoded = content.encode("utf-8")

        def apply_write() -> str:
            workspace.atomic_write(resolved, encoded, expected_revision=expected_revision)
            return f"Wrote {len(encoded)} bytes to {path}"

        if task_workspace is None:
            return apply_write()
        return task_workspace.perform_text_mutation(
            path,
            operation="write_file",
            selector="whole",
            change=content,
            action=apply_write,
        )

    def edit_file(path: str, find: str, replace: str) -> str:
        """Replace the unique exact ``find`` occurrence in ``path`` with ``replace``.

        Fails if ``find`` appears zero or more than one time so the model cannot
        accidentally edit the wrong location. The replacement is atomic via the
        same ``write_file`` machinery.
        """

        resolved = workspace.resolve_for_mutation(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        if not find:
            raise ValueError("find must not be empty")
        original = resolved.read_bytes().decode("utf-8")
        count = original.count(find)
        if count == 0:
            raise ValueError(f"0 matches for find in {path}")
        if count > 1:
            raise ValueError(f"{count} matches for find in {path}")
        updated = original.replace(find, replace, 1)
        encoded = updated.encode("utf-8")
        expected_revision = f"sha256:{hashlib.sha256(original.encode('utf-8')).hexdigest()}"

        def apply_edit() -> str:
            workspace.atomic_write(resolved, encoded, expected_revision=expected_revision)
            return f"Edited {path}: replaced 1 occurrence"

        if task_workspace is None:
            return apply_edit()
        return task_workspace.perform_text_mutation(
            path,
            operation="edit_file",
            selector=f"anchor:{find}",
            change=replace,
            action=apply_edit,
        )

    async def run_command(
        argv: list[str],
        *,
        cwd: str = ".",
        timeout: float | None = None,  # noqa: ASYNC109  # tool-arg, not a task wait
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """在 max_timeout 秒上限内直接运行 argv，不经过 shell。

        子进程在独立进程组中运行，超时或取消时会一并清理后代进程。stdout 和 stderr 会并发
        读取，每个流只在内存中保留固定大小的头尾内容，避免超大输出耗尽运行时内存。
        """

        if not argv:
            raise ValueError("argv must contain at least one element")
        resolved_cwd = workspace.resolve(cwd)
        if not resolved_cwd.is_dir():
            raise NotADirectoryError(f"cwd is not a directory: {cwd}")
        effective_timeout = max_timeout if timeout is None else min(timeout, max_timeout)
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")

        return await run_prepared_command(
            sandbox.prepare(argv, cwd=resolved_cwd, overrides=env),
            argv=argv, resolved_cwd=resolved_cwd, cwd=cwd,
            timeout_seconds=effective_timeout, sandbox_config=sandbox.config,
        )

    return [
        ToolSpec(
            write_file,
            description=(
                "把 UTF-8 文本写入工作区内的相对路径。缺失的父目录会自动创建；已有文件默认拒绝"
                "覆盖，只有 overwrite=true 时才替换。用户未指定交付路径时，报告或导出写入 outputs/。"
            ),
            risk=Risk.WRITE,
            effect_kind=EffectKind.MUTATION,
        ),
        ToolSpec(
            edit_file,
            description=(
                "在工作区文件中把唯一一次精确匹配的 find 替换为 replace。匹配为零或多于一次时"
                "安全失败；应先读取或搜索文件以取得唯一、稳定的上下文片段。"
            ),
            risk=Risk.WRITE,
            effect_kind=EffectKind.MUTATION,
        ),
        ToolSpec(
            run_command,
            description=(
                (run_command.__doc__ or "")
                + f"\n执行策略: sandbox.mode={sandbox.config.mode}; "
                + f"命令允许联网={sandbox.config.mode == 'disabled' or sandbox.config.network}。"
                + "此策略只适用于本子进程，不适用于单独配置的 MCP/Web 工具。命令失败后检查 "
                + "stderr 和 exit_code，不要据此推断全局网络可用性。"
                + f"已知 Python 解释器: {sys.executable}。"
                + "工作区外已发现的 Skill 请使用 load_skill/read_skill_resource。"
            ),
            risk=Risk.EXECUTE,
            effect_kind=EffectKind.EXECUTION,
            timeout=max_timeout,
        ),
    ]


async def run_prepared_command(
    prepared: PreparedSandboxCommand,
    *,
    argv: list[str],
    resolved_cwd: Path,
    cwd: str,
    timeout_seconds: float,
    sandbox_config: SandboxConfig,
    stdin_data: bytes | None = None,
) -> dict[str, Any]:
    """Run an already-authorized command with bounded output and process-tree cleanup."""
    start = time.monotonic()
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *prepared.argv, cwd=str(resolved_cwd),
        stdin=(asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=prepared.env, start_new_session=True,
    ))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        try:
            process = await spawn
            await _terminate_process_group(process)
            await _drain_streams(process, BoundedCollector(), BoundedCollector())
        finally:
            prepared.cleanup()
        raise
    except BaseException:
        prepared.cleanup()
        raise

    stdout_collector = BoundedCollector()
    stderr_collector = BoundedCollector()
    timed_out = False
    cancelled = False
    try:
        try:
            await asyncio.wait_for(
                _communicate(
                    process,
                    stdout_collector,
                    stderr_collector,
                    stdin_data=stdin_data,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            timed_out = True
            await _terminate_process_group(process)
            # Drain whatever remains after terminating so the pipes close
            # and the child can be reaped; the collectors stay bounded.
            await _drain_streams(process, stdout_collector, stderr_collector)
        exit_code = process.returncode
    except asyncio.CancelledError:
        cancelled = True
        await _terminate_process_group(process)
        try:
            await _drain_streams(process, stdout_collector, stderr_collector)
        except Exception:
            pass
        raise
    finally:
        # Always reap the child so we don't leak zombies even on cancellation.
        if process.returncode is None:
            try:
                await _terminate_process_group(process)
                await process.wait()
            except Exception:
                pass
        prepared.cleanup()
        if cancelled and process.returncode is None:
            try:
                await process.wait()
            except Exception:
                pass

    elapsed = time.monotonic() - start
    return {
        "argv": list(argv),
        "cwd": str(Path(cwd)),
        "exit_code": exit_code,
        "stdout": stdout_collector.render(),
        "stderr": stderr_collector.render(),
        "stdout_total_bytes": stdout_collector.total_bytes,
        "stderr_total_bytes": stderr_collector.total_bytes,
        "elapsed_seconds": round(elapsed, 3),
        "timed_out": timed_out,
        "stdout_truncated": stdout_collector.truncated,
        "stderr_truncated": stderr_collector.truncated,
        "sandbox": {
            "mode": sandbox_config.mode,
            "network_allowed": sandbox_config.mode == "disabled" or sandbox_config.network,
            "scope": "this command only; MCP and web tools have separate permissions",
        },
    }



async def _drain_stream(stream: asyncio.StreamReader | None, collector: BoundedCollector) -> None:
    """Read ``stream`` to EOF, feeding each chunk to ``collector``.

    The collector bounds memory as it goes, so this loop can run to completion
    over a multi-megabyte pipe without ever holding the full output. Reading
    in a loop (rather than ``read()``) keeps the pipe drained so a full pipe
    buffer can't block the child's writes.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        collector.feed(chunk)


async def _drain_streams(
    process: asyncio.subprocess.Process,
    stdout_collector: BoundedCollector,
    stderr_collector: BoundedCollector,
) -> None:
    """Drain stdout and stderr concurrently and wait for the child to exit.

    Both streams must be drained at the same time — if only stdout were read
    and stderr filled its OS pipe buffer, the child would block on its write
    and never terminate. Gathering the two drainers plus ``wait()`` ensures
    neither pipe stalls the process.
    """
    await asyncio.gather(
        _drain_stream(process.stdout, stdout_collector),
        _drain_stream(process.stderr, stderr_collector),
        process.wait(),
    )


async def _write_stdin(stream: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stream is None:
        return
    try:
        if data is not None:
            stream.write(data)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        # A hook/command may intentionally exit without consuming the complete
        # payload. Its exit status remains authoritative.
        pass
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _communicate(
    process: asyncio.subprocess.Process,
    stdout_collector: BoundedCollector,
    stderr_collector: BoundedCollector,
    *,
    stdin_data: bytes | None,
) -> None:
    await asyncio.gather(
        _write_stdin(process.stdin, stdin_data),
        _drain_stream(process.stdout, stdout_collector),
        _drain_stream(process.stderr, stderr_collector),
        process.wait(),
    )


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Terminate the process tree with taskkill on Windows or POSIX signals."""
    if sys.platform == "win32":
        # Use the system executable, never a workspace/PATH-provided program.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                str(taskkill), "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except (OSError, TimeoutError):
            pass
        finally:
            if killer is not None and killer.returncode is None:
                try:
                    killer.kill()
                except ProcessLookupError:
                    pass
                await killer.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        return
    # start_new_session makes the original PID the group ID. A leader may
    # already have exited while descendants still hold stdout/stderr open.
    pgid = process.pid
    for signal_name in ("SIGTERM", "SIGKILL"):
        sig = getattr(signal, signal_name)
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            # Fall back to direct signal if we cannot reach the whole group.
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                return
        if signal_name == "SIGTERM":
            try:
                await asyncio.wait_for(process.wait(), timeout=0.2)
            except TimeoutError:
                continue


__all__ = ["MAX_OUTPUT_BYTES", "BoundedCollector", "build_capability_specs"]
