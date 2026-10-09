"""Compare effort/concurrency on repeatable read-only fixtures; dry-run by default.

uv run python scripts/benchmark_agent_loop.py --config agent.yaml --tasks tasks.json
Add --execute --output results.json to use the configured paid provider.
Tasks: [{"id": "lookup", "prompt": "...", "files": {"a.txt": "..."},
         "expected_substrings": ["answer"]}]. Each trial gets a fresh workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from lumen.agents.types import AgentEventKind, AgentProfile, AgentStatus
from lumen.config import AppConfig, PromptConfig, ToolsConfig, load_config
from lumen.headless import run_headless
from lumen.reasoning import ReasoningLevel, apply_reasoning, resolve_reasoning
from lumen.resources import ResourceManager


class BenchmarkTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    files: dict[str, str] = Field(default_factory=dict)
    expected_substrings: list[str] = Field(default_factory=list)
    mode: Literal["read", "edit"] = "read"
    expected_files: dict[str, str | None] = Field(default_factory=dict)
    allowed_changed_paths: list[str] = Field(default_factory=list)

    @field_validator("files", "expected_files")
    @classmethod
    def relative_files(cls, files: dict[str, Any]) -> dict[str, Any]:
        for name in files:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0].startswith("."):
                raise ValueError("fixture files must stay inside the trial workspace")
        return files

    @field_validator("allowed_changed_paths")
    @classmethod
    def changed_paths(cls, values: list[str]) -> list[str]:
        cls.relative_files(dict.fromkeys(values))
        return values


def file_digests(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                result[path.relative_to(workspace).as_posix()] = "symlink"
        dirs[:] = sorted(name for name in dirs if not (
            Path(directory) == workspace and name in {".sessions", ".worktrees", ".lumen"}
        ))
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                result[path.relative_to(workspace).as_posix()] = "symlink"
            else:
                result[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def grade_files(task: BenchmarkTask, before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    changes = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    allowed = set(task.allowed_changed_paths) if task.mode == "edit" else set()
    violations = sorted(set(changes) - allowed)
    mismatches = [name for name, content in task.expected_files.items() if after.get(name) != (
        hashlib.sha256(content.encode()).hexdigest() if content is not None else None
    )]
    return {"changed_paths": changes, "scope_violations": violations, "file_mismatches": mismatches,
            "file_grade_pass": not violations and not mismatches}


OFFLINE_CASES = (
    "tests/test_builtin_tools.py::test_search_streams_stops_early_and_preserves_rooted_glob",
    "tests/test_capability_tools.py::test_journaled_edit_preserves_exact_anchor_bytes",
    "tests/test_context_engine.py::test_in_run_compaction_rolls_up_and_reloads_without_duplicate_messages",
    "tests/test_context_engine.py::test_per_step_window_receiptizes_large_current_run_tool_output",
    "tests/test_runtime.py::test_runtime_lumen_loop_cancellation_preserves_request_evidence",
    "tests/test_runtime.py::test_runtime_lumen_loop_reuses_shared_side_effect_recovery_receipt",
    "tests/test_effect_recovery.py::test_external_receipt_is_durable_before_dispatch_and_reconciles_uncertainty",
    "tests/test_workspace_host.py::test_plan_review_is_revisioned_persistent_and_idempotent",
    "tests/test_agent_execution.py::test_worktree_execution_preserves_parent_and_rejects_dirty_import",
    "tests/test_workspace_host.py::test_host_direct_command_denied_by_user",
    "tests/test_event_journal.py::test_bounded_replay_preserves_sequences_and_slow_reconnects",
    "tests/test_turn_restore.py::test_restore_previews_precise_turn_files_requires_confirmation_and_is_idempotent",
    "tests/test_process_recovery.py::test_hard_kill_recovers_only_durable_boundaries_without_duplicate_effects",
)


def offline_report() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="lumen-offline-eval-") as directory:
        junit = Path(directory) / "results.xml"
        result = subprocess.run([sys.executable, "-m", "pytest", *OFFLINE_CASES,
                                 f"--junitxml={junit}", "-q"], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=180)
        cases = []
        if junit.exists():
            for item in ET.parse(junit).iter("testcase"):
                cases.append({"name": item.attrib["name"], "seconds": float(item.attrib.get("time", "0")),
                    "passed": item.find("failure") is None and item.find("error") is None,
                    "skipped": item.find("skipped") is not None})
        return {"executed_provider": False, "case_count": len(cases), "passed": result.returncode == 0,
                "scope": "Deterministic contract scenarios; no paid model quality estimate.", "cases": cases}


def summarize_requests(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    requests = [item for item in diagnostics if item.get("kind") == "request_observed"]
    return {
        "observed_attempts": len(requests),
        "control_only_ratio": sum(bool(item.get("control_only")) for item in requests) / len(requests)
        if requests else None,
        "request_seconds": [item.get("elapsed_seconds") for item in requests],
        "first_event_seconds": [item.get("first_event_seconds") for item in requests],
        "thinking_characters": sum(item.get("thinking_characters", 0) for item in requests),
        "retry_attempts": len(requests) - len({item.get("request_index") for item in requests}),
    }


async def trial(config: AppConfig, task: BenchmarkTask, effort: ReasoningLevel,
                concurrency: int) -> dict[str, Any]:
    # Fresh fixtures prevent one trial's Session/context or file edits affecting the next.
    with tempfile.TemporaryDirectory(prefix="lumen-loop-benchmark-") as directory:
        workspace = Path(directory)
        for name, content in task.files.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        before = file_digests(workspace)
        isolated = config.model_copy(update={
            "agent": config.agent.model_copy(
                update={"prompt": PromptConfig(mode="preset"), "skills_enabled": False}
            ),
            "agents": config.agents.model_copy(update={
                "enabled": True, "autonomy": "adaptive", "default_agent": "explorer",
                "max_concurrency": concurrency, "worktree_root": workspace / ".worktrees",
            }),
            "sessions": config.sessions.model_copy(update={"directory": workspace / ".sessions"}),
            "tools": ToolsConfig(builtins=["read_file", "list_directory", "search_text"]
                + (["write_file", "edit_file"] if task.mode == "edit" else [])),
            "permissions": config.permissions.model_copy(update={"default_mode": "auto"}),
            "hooks": [], "mcp_servers": {}, "project_trusted": False,
            "memory": config.memory.model_copy(update={"use": False, "learn": False}),
            "live": config.live.model_copy(update={"enabled": False}),
        })
        resources = ResourceManager(isolated, workspace=workspace)
        # Profiles discovered from the user's home cannot change the experimental model/tool policy.
        profiles = {"explorer": AgentProfile(
            name="explorer", description="只读测试夹具调查",
            instructions="检查分配的测试夹具任务并报告证据。不要修改文件。",
            tools=("read_file", "list_directory", "search_text"),
        )}
        resources.agent_profiles = profiles
        resources.agent_orchestrator.profiles = profiles
        resources.set_startup_reasoning(effort)
        started = time.monotonic()
        outcome = await run_headless(resources, task.prompt, output_format="json", stdout=io.StringIO())
        elapsed = time.monotonic() - started
        session = resources.session_repository.load(outcome.session_id)
        diagnostics = [item for turn in session.turns for item in turn.diagnostics]
        active: set[str] = set()
        peak = 0
        for event in session.agent_state.events:
            if event.kind is AgentEventKind.STARTED:
                active.add(event.agent_id)
                peak = max(peak, len(active))
            elif event.status not in {AgentStatus.RUNNING, AgentStatus.APPROVAL_PENDING}:
                active.discard(event.agent_id)
        grade = grade_files(task, before, file_digests(workspace))
        completed = outcome.error is None and bool(session.turns) and session.turns[-1].status == "completed"
        answer_pass = all(value in outcome.text for value in task.expected_substrings)
        success = completed and grade["file_grade_pass"] and answer_pass
        return {
            "task": task.id, "effort": effort.value, "concurrency": concurrency,
            "elapsed_seconds": elapsed, "completed": completed, "task_success": success,
            "false_completion": completed and not success, **grade,
            "total_usage_complete": bool(outcome.usage) and not session.agent_state.threads, "cost": None,
            "expected_substrings_pass": all(value in outcome.text for value in task.expected_substrings)
            if task.expected_substrings else None,
            "parent_usage": outcome.usage, "parent_requests": outcome.request_count,
            "parent_tool_calls": outcome.tool_call_count,
            "agent_peak_concurrency": peak,
            "child_usage": [thread.usage for thread in session.agent_state.threads],
            "reasoning": resolve_reasoning(resources.active_model_config(), effort).model_dump(mode="json"),
            **summarize_requests(diagnostics),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--micro", action="store_true", help="Run offline runtime microbenchmarks")
    parser.add_argument("--offline", action="store_true", help="Run trusted deterministic contract scenarios")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--efforts", nargs="+", type=ReasoningLevel,
                        default=[ReasoningLevel.PROVIDER_DEFAULT, ReasoningLevel.LOW,
                                 ReasoningLevel.MEDIUM, ReasoningLevel.HIGH])
    parser.add_argument("--concurrency", nargs="+", type=int, choices=range(1, 9), default=[1, 3])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--execute", action="store_true", help="Actually call the configured model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.offline:
        if args.micro or args.execute or args.config or args.tasks:
            parser.error("--offline cannot be combined with micro/provider arguments")
        report = offline_report()
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(payload)
        else:
            print(payload, end="")
        if not report["passed"]:
            raise SystemExit(1)
        return
    if args.micro:
        if args.execute or args.config or args.tasks:
            parser.error("--micro is offline and cannot be combined with provider arguments")
        if args.samples < 1:
            parser.error("samples must be positive")
        from benchmark_runtime import run
        payload = json.dumps(run(args.samples), ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(payload)
        else:
            print(payload, end="")
        return
    if args.config is None or args.tasks is None:
        parser.error("provider comparison requires --config and --tasks")
    if args.repeats < 1 or (args.execute and args.output is None):
        parser.error("repeats must be positive; --execute requires --output")
    tasks = TypeAdapter(list[BenchmarkTask]).validate_json(args.tasks.read_text())
    if not tasks or len({task.id for task in tasks}) != len(tasks):
        parser.error("provide nonempty tasks with unique IDs")
    config = load_config(args.config)
    model_name = config.agent.default_model
    model = config.agent.model or config.agent.models[model_name or next(iter(config.agent.models))]
    for effort in args.efforts:
        apply_reasoning(model.settings, resolve_reasoning(model, effort))
    matrix = [(task, effort, concurrency, repeat)
              for repeat in range(args.repeats) for task in tasks
              for effort in args.efforts for concurrency in args.concurrency]
    random.Random(0).shuffle(matrix)
    report: dict[str, Any] = {
        "model": model.id, "executed": args.execute, "trial_count": len(matrix), "results": [],
        "scope": "Explicit read/edit fixtures, exact file/scope grading. Parent/child usage is separate. "
        "Substring checks are a quality proxy; absent usage/cost is unknown, not zero. "
        "Provider cache, rate limits and infrastructure can affect comparisons.",
    }
    if args.execute:
        async def run() -> None:
            for task, effort, concurrency, repeat in matrix:
                row = await trial(config, task, effort, concurrency)
                report["results"].append({**row, "repeat": repeat + 1})
                # Persist completed trials even if a later provider request fails.
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        asyncio.run(run())
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
