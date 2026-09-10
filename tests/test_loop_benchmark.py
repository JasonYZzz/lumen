from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from test_reasoning import manager

from lumen.config import ModelSettingsConfig
from lumen.reasoning import ReasoningLevel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_agent_loop import BenchmarkTask, summarize_requests, trial


def test_benchmark_counts_retries_without_confusing_global_attempts() -> None:
    report = summarize_requests([
        {"kind": "request_observed", "request_index": index, "model_attempts": attempt,
         "control_only": index == 1, "thinking_characters": 10}
        for index, attempt in [(1, 1), (1, 2), (2, 3)]
    ])
    assert report["retry_attempts"] == 1
    assert report["control_only_ratio"] == 2 / 3
    assert report["thinking_characters"] == 30
    assert summarize_requests([])["control_only_ratio"] is None


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "."])
def test_benchmark_rejects_fixture_path_escape(name: str) -> None:
    with pytest.raises(ValidationError):
        BenchmarkTask(id="test", prompt="read", files={name: "text"})


async def test_benchmark_exercises_real_host_without_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def model(_config: ModelSettingsConfig) -> TestModel:
        return TestModel(call_tools=[], custom_output_text="checked fixture")

    monkeypatch.setattr("lumen.resources.build_model", model)
    report = await trial(manager(tmp_path).config,
                         BenchmarkTask(id="read", prompt="Inspect the fixture", files={"input.txt": "hello"},
                                       expected_substrings=["checked"]), ReasoningLevel.MEDIUM, 3)
    assert report["completed"] is True
    assert report["expected_substrings_pass"] is True
    assert report["parent_requests"] == 1
    assert report["agent_peak_concurrency"] == 0
    assert report["reasoning"]["effective"] == "medium"
    assert "hello" not in str(report)
