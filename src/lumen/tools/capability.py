"""Workspace-scoped write, exact edit, and argv command tools.

These are the workspace capability tools the model uses to actually act on the
project: writing files, editing files by exact match, and running commands
without going through a shell. All three are workspace-scoped and the model is
gated by the standard permission system before any of them can execute.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lumen.config import SandboxConfig
from lumen.sandbox import SandboxRunner
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
        original = resolved.read_text(encoding="utf-8")
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
        """Run ``argv`` (no shell) bounded to ``max_timeout`` seconds.

        The child runs in its own process group so timeouts and cancellations
        can reap any descendants too. stdout and stderr are drained
        concurrently by :class:`BoundedCollector` so only a fixed head+tail of
        each stream is kept in memory — a runaway command writing 100 MB
        cannot exhaust the run.
        """

        if not argv:
            raise ValueError("argv must contain at least one element")
        resolved_cwd = workspace.resolve(cwd)
        if not resolved_cwd.is_dir():
            raise NotADirectoryError(f"cwd is not a directory: {cwd}")
        effective_timeout = max_timeout if timeout is None else min(timeout, max_timeout)
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")

        prepared = sandbox.prepare(argv, cwd=resolved_cwd, overrides=env)
        start = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *prepared.argv,
                cwd=str(resolved_cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=prepared.env,
                start_new_session=True,
            )
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
                    _drain_streams(process, stdout_collector, stderr_collector),
                    timeout=effective_timeout,
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
        }

    return [
        ToolSpec(write_file, risk=Risk.WRITE, effect_kind=EffectKind.MUTATION),
        ToolSpec(edit_file, risk=Risk.WRITE, effect_kind=EffectKind.MUTATION),
        ToolSpec(run_command, risk=Risk.EXECUTE, effect_kind=EffectKind.EXECUTION, timeout=max_timeout),
    ]


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


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Best-effort SIGTERM of the process group, then SIGKILL if still alive."""

    if process.returncode is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for signal_name in ("SIGTERM", "SIGKILL"):
        if process.returncode is not None:
            return
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
                return
            except TimeoutError:
                continue


__all__ = ["MAX_OUTPUT_BYTES", "BoundedCollector", "build_capability_specs"]
