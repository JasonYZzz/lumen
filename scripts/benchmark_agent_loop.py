"""Compare effort/concurrency on repeatable read-only fixtures; dry-run by default.

uv run python scripts/benchmark_agent_loop.py --config agent.yaml --tasks tasks.json
Add --execute --output results.json to use the configured paid provider.
Tasks: [{"id": "lookup", "prompt": "...", "files": {"a.txt": "..."},
         "expected_substrings": ["answer"]}]. Each trial gets a fresh workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tempfile
import time
from pathlib import Path
from typing import Any

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

    @field_validator("files")
    @classmethod
    def relative_files(cls, files: dict[str, str]) -> dict[str, str]:
        for name in files:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("fixture files must stay inside the trial workspace")
        return files


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
        isolated = config.model_copy(update={
            "agent": config.agent.model_copy(
                update={"prompt": PromptConfig(mode="preset"), "skills_enabled": False}
            ),
            "agents": config.agents.model_copy(update={
                "enabled": True, "autonomy": "adaptive", "default_agent": "explorer",
                "max_concurrency": concurrency, "worktree_root": workspace / ".worktrees",
            }),
            "sessions": config.sessions.model_copy(update={"directory": workspace / ".sessions"}),
            "tools": ToolsConfig(builtins=["read_file", "list_directory", "search_text"]),
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
        return {
            "task": task.id, "effort": effort.value, "concurrency": concurrency,
            "elapsed_seconds": elapsed, "completed": outcome.error is None
            and bool(session.turns) and session.turns[-1].status == "completed",
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
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--efforts", nargs="+", type=ReasoningLevel,
                        default=[ReasoningLevel.PROVIDER_DEFAULT, ReasoningLevel.LOW,
                                 ReasoningLevel.MEDIUM, ReasoningLevel.HIGH])
    parser.add_argument("--concurrency", nargs="+", type=int, choices=range(1, 9), default=[1, 3])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--execute", action="store_true", help="Actually call the configured model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
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
    report: dict[str, Any] = {
        "model": model.id, "executed": args.execute, "trial_count": len(matrix), "results": [],
        "scope": "Read-only fixtures; parent request metrics and separate child usage. "
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
