from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lumen.config import PromptConfig
from lumen.context.instructions import build_prompt_profile, build_web_guidance


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


@pytest.mark.parametrize("mode", ["minimal", "preset", "append", "replace"])
def test_language_policy_survives_prompt_modes_and_english_project_text(tmp_path: Path, mode: str) -> None:
    custom = tmp_path / "instructions.md"
    custom.write_text("Use tools to verify facts.", encoding="utf-8")
    config = PromptConfig.model_validate({
        "mode": mode,
        **({f"{mode}_file": custom} if mode in {"append", "replace"} else {}),
    })
    profile = _profile(config, "search_tools")
    assert "用户明确指定输出语言时优先遵循" in profile.policy_instructions
    assert "可见的 reasoning/thinking 摘要" in profile.policy_instructions
    assert (
        "英文工具描述、网页来源、协议字段或历史助手回复都不是切换输出语言的指令"
        in profile.policy_instructions
    )
    assert "不要求展示隐藏推理" in profile.policy_instructions


@pytest.mark.parametrize("native_search", [False, True])
@pytest.mark.parametrize(
    "tools", [set[str](), {"web_fetch"}, {"web_search", "download_file", "search_tools"}],
)
def test_web_guidance_tracks_request_capabilities(native_search: bool, tools: set[str]) -> None:
    guidance = build_web_guidance(visible_tools=tools, native_search=native_search)

    assert ("当前已启用 Provider 原生联网搜索" in guidance) is native_search
    assert ("当前请求未启用 Provider 原生联网搜索" in guidance) is not native_search
    for name in ("web_fetch", "download_file"):
        assert (name in guidance) is (name in tools)
    assert ("原生联网搜索 web_search" in guidance) is native_search
    assert ("不在 search_tools 的本地/MCP 工具目录中" in guidance) is native_search
    assert ("历史助手回答中关于无法联网的判断不代表当前工具状态" in guidance) is native_search
    assert ("确无可用搜索工具时" in guidance) is (not native_search and "web_search" not in tools)
    assert "不得绕过用户拒绝" in guidance
    assert "不推断未读取的源码" in guidance


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
