from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lumen.config import PromptConfig
from lumen.context.instructions import build_prompt_profile


def _profile(config: PromptConfig, *tools: str):
    return build_prompt_profile(
        config,
        visible_tools=set(tools),
        agents_enabled=True,
        agent_autonomy="adaptive",
    )


def test_prompt_modes_have_distinct_explicit_semantics(tmp_path: Path) -> None:
    append_file = tmp_path / "append.md"
    replace_file = tmp_path / "replace.md"
    append_file.write_text("项目追加指令", encoding="utf-8")
    replace_file.write_text("完全自定义身份", encoding="utf-8")

    minimal = _profile(PromptConfig(mode="minimal"))
    preset = _profile(PromptConfig(mode="preset"))
    appended = _profile(PromptConfig(mode="append", append_file=append_file))
    replaced = _profile(PromptConfig(mode="replace", replace_file=replace_file))

    assert len(minimal.instructions) < len(preset.instructions)
    assert "项目追加指令" in appended.instructions
    assert "你是 Lumen" in appended.instructions
    assert "完全自定义身份" in replaced.system_instructions
    assert "你是 Lumen" not in replaced.system_instructions
    assert "set_plan" in replaced.policy_instructions
    assert "可见推理" in replaced.policy_instructions
    assert "用户当前请求的主要语言" in replaced.policy_instructions
    assert [source.origin for source in appended.sources][-1] == str(append_file)


def test_prompt_tool_guidance_is_only_added_for_visible_tools() -> None:
    without_tools = _profile(PromptConfig())
    with_tools = _profile(PromptConfig(), "load_skill", "search_tools")

    assert "available_skills" not in without_tools.instructions
    assert "available_skills" in with_tools.instructions
    assert "search_tools" in with_tools.instructions


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "append"},
        {"mode": "replace"},
        {"mode": "preset", "append_file": "extra.md"},
    ],
)
def test_prompt_config_rejects_ambiguous_file_combinations(value: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        PromptConfig.model_validate(value)
