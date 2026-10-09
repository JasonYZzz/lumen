"""Offline runtime microbenchmarks, also available from benchmark_agent_loop --micro."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from lumen.application.events import EventJournal
from lumen.context.artifacts import ArtifactStore
from lumen.events import TextDelta
from lumen.sessions import SessionRepository
from lumen.tools.builtin import build_builtin_specs


def environment() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    source = hashlib.sha256()
    for path in sorted((root / "src/lumen").rglob("*.py")):
        source.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    def version(command: list[str]) -> str | None:
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False,
                                    timeout=10)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return {
        "commit": version(["git", "rev-parse", "HEAD"]),
        "source_sha256": source.hexdigest(),
        "measurement_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "os": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "node": version(["node", "--version"]), "pnpm": version(["pnpm", "--version"]),
        "locks": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (root / "uv.lock", root / "src/web/pnpm-lock.yaml") if path.exists()},
    }


def measure(operation: Callable[[], object], samples: int) -> dict[str, Any]:
    operation()  # warmup, excluded from observations
    rows: list[dict[str, Any]] = []
    read_bytes, read_calls, decodes = 0, 0, 0
    original_read = Path.read_bytes
    original_os_read = os.read
    original_loads = json.loads
    def counted_read(path: Path) -> bytes:
        nonlocal read_bytes, read_calls
        result = original_read(path)
        read_bytes += len(result)
        read_calls += 1
        return result
    def counted_os_read(fd: int, size: int) -> bytes:
        nonlocal read_bytes, read_calls
        result = original_os_read(fd, size)
        read_bytes += len(result)
        read_calls += 1
        return result
    def counted_loads(*args: Any, **kwargs: Any) -> Any:
        nonlocal decodes
        decodes += 1
        return original_loads(*args, **kwargs)
    for _ in range(samples):
        read_bytes = read_calls = decodes = 0
        with patch.object(Path, "read_bytes", counted_read), patch.object(os, "read", counted_os_read), \
                patch.object(json, "loads", counted_loads):
            started = time.perf_counter()
            operation()
            elapsed = time.perf_counter() - started
        rows.append({"seconds": elapsed, "read_bytes": read_bytes,
                     "read_calls": read_calls, "json_decodes": decodes})
    tracemalloc.start()
    operation()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    times = sorted(row["seconds"] for row in rows)
    return {"samples": rows, "median_seconds": statistics.median(times),
            "p95_seconds": times[math.ceil(len(times) * .95) - 1], "peak_python_bytes": peak}


async def artifact_sample(root: Path, size: int, index: int, *, indexed: bool) -> dict[str, Any]:
    store = ArtifactStore(root / str(index))
    if not indexed:
        # Exercise the existing best-effort fallback without changing blob durability.
        class UnavailableIndex:
            def index_artifact(self, *_args: object) -> None:
                raise sqlite3.OperationalError("synthetic unavailable index")
        store._search_index = UnavailableIndex()  # type: ignore[assignment]
    content = b"synthetic benchmark output\n" * (size // 27 + 1)
    content = content[:size]
    delays: list[float] = []
    finished = False
    async def heartbeat() -> None:
        previous = time.perf_counter()
        while not finished:
            await asyncio.sleep(0)
            now = time.perf_counter()
            delays.append(now - previous)
            previous = now
    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    started = time.perf_counter()
    # Use the async boundary if the store exposes it; compare the same operation
    # against the original synchronous implementation on earlier revisions.
    if hasattr(store, "astore"):
        await store.astore(content)
    else:
        store.store(content)
    elapsed = time.perf_counter() - started
    await asyncio.sleep(0)
    finished = True
    await task
    if store._search_index is not None and hasattr(store._search_index, "close"):
        store._search_index.close()
    return {"seconds": elapsed, "loop_lag_seconds": max(delays, default=0), "bytes": len(content)}


def run(samples: int = 30) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    report: dict[str, Any] = {"schema_version": 1, "executed": False,
                              "environment": environment(), "sessions": [], "artifacts": [], "search": {}}
    with tempfile.TemporaryDirectory(prefix="lumen-runtime-benchmark-") as directory:
        root = Path(directory)
        for count in (20, 200, 2000):
            repo = SessionRepository(root / f"sessions-{count}")
            session = repo.create(agent_name="benchmark", model_id="offline")
            for index in range(count):
                repo.append_turn(session.id, user_input="synthetic", messages=[
                    ModelRequest(parts=[UserPromptPart(f"fixture {index}")]),
                    ModelResponse(parts=[TextPart("synthetic response")]),
                ], approvals=[], usage={}, status="completed")
            def cold(repo: SessionRepository = repo, session_id: str = session.id) -> object:
                repo.clear_projection_cache()
                return repo.load(session_id)
            row: dict[str, Any] = {"turns": count, "journal_bytes": session.path.stat().st_size,
                                   "cold": measure(cold, samples),
                                   "hot": measure(lambda repo=repo, sid=session.id: repo.load(sid), samples),
                                   "list": measure(repo.list, samples),
                                   "page": measure(
                                       lambda repo=repo, sid=session.id: repo.load_turn_page(sid, limit=20),
                                       samples)}
            def appended(repo: SessionRepository = repo, session_id: str = session.id) -> object:
                repo.append_turn(session_id, user_input="synthetic", messages=[],
                                 approvals=[], usage={}, status="completed")
                return repo.load(session_id)
            row["append_load"] = measure(appended, samples)
            report["sessions"].append(row)
        for size in (1, 10, 64):
            for indexed in (False, True):
                with tempfile.TemporaryDirectory(dir=root) as store_root:
                    async def collect(size: int = size, indexed: bool = indexed,
                                      store_root: str = store_root) -> list[dict[str, Any]]:
                        await artifact_sample(Path(store_root), size * 1024 * 1024, -1, indexed=indexed)
                        rows = []
                        for index in range(samples):
                            rows.append(await artifact_sample(Path(store_root), size * 1024 * 1024,
                                                              index, indexed=indexed))
                        return rows
                    rows = asyncio.run(collect())
                    report["artifacts"].append({"mib": size, "indexed": indexed, "samples": rows,
                        "median_seconds": statistics.median(row["seconds"] for row in rows),
                        "p95_loop_lag_seconds": sorted(row["loop_lag_seconds"] for row in rows)[
                            math.ceil(samples * .95) - 1]})
        tree = root / "tree"
        tree.mkdir()
        (tree / "a.txt").write_text("needle\n" * 1000)
        for index in range(100):
            nested = tree / f"dir-{index:03}" / "deep" / "deeper"
            nested.mkdir(parents=True)
            (nested / "file.txt").write_text("no hit\n" * 100)
        ignored = tree / "node_modules" / "nested"
        ignored.mkdir(parents=True)
        (ignored / "ignored.txt").write_text("needle\n")
        specs = {spec.name: spec for spec in build_builtin_specs(tree)}
        report["search"] = {
            "depth_one": measure(lambda: specs["list_directory"].function(depth=1), samples),
            "early_match": measure(
                lambda: specs["search_text"].function(query="needle", max_results=1), samples),
            "no_match": measure(lambda: specs["search_text"].function(query="absent"), samples),
        }
        async def events() -> dict[str, Any]:
            journal = EventJournal(session_id="synthetic", run_id="synthetic")
            for _ in range(10000):
                await journal.append(TextDelta("synthetic " * 100))
            await journal.close()
            return {"published_events": 10000, "retained_events": len(journal.events),
                    "retained_payload_bytes": sum(
                        len(json.dumps(event.data).encode()) for event in journal.events)}
        report["events"] = asyncio.run(events())
    return report
