"""Single authority for stable model instructions and their provenance."""

# Model-facing Chinese prose is kept as authored; ASCII punctuation makes it harder to read.
# ruff: noqa: E501, RUF001

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from lumen.branding import FRAMEWORK_NAME

PROMPT_PRESET_VERSION = "lumen-2026-09-09"


class PromptConfiguration(Protocol):
    mode: Literal["minimal", "preset", "append", "replace"]
    preset: Literal["lumen"]
    append_file: Path | None
    replace_file: Path | None


@dataclass(frozen=True, slots=True)
class InstructionSource:
    origin: str
    role: Literal["system", "policy"]
    text: str
    revision: str


@dataclass(frozen=True, slots=True)
class PromptProfile:
    """Resolved stable instructions sent on every request for one runtime."""

    mode: str
    preset: str | None
    version: str
    sources: tuple[InstructionSource, ...]

    @property
    def system_instructions(self) -> str:
        return "\n\n".join(source.text for source in self.sources if source.role == "system")

    @property
    def policy_instructions(self) -> str:
        return "\n\n".join(source.text for source in self.sources if source.role == "policy")

    @property
    def instructions(self) -> str:
        return "\n\n".join(
            value for value in (self.system_instructions, self.policy_instructions) if value
        )

    def report(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "preset": self.preset,
            "version": self.version,
            "digest": _revision(self.instructions),
            "characters": len(self.instructions),
            "sources": [
                {
                    "origin": source.origin,
                    "role": source.role,
                    "revision": source.revision,
                    "characters": len(source.text),
                }
                for source in self.sources
            ],
        }


MINIMAL_INSTRUCTIONS = f"""你是 {FRAMEWORK_NAME}，在用户的工作区内完成任务。
按需使用当前可用工具，并以工具结果和工作区证据为依据；不要编造执行或验证结果。
把结论和必要依据直接告诉用户，不输出私有思维链。"""

PRESET_INSTRUCTIONS = f"""你是 {FRAMEWORK_NAME}，一个通用、可配置的 Agent framework，工作在用户的工作区内。
{FRAMEWORK_NAME} 是你的产品身份；当前模型只是可替换的推理 Provider。被问及身份时，应分别说明 framework 和运行时给出的当前模型。

判断任务是否需要工具；需要行动或核实事实时，使用当前可用工具并以实际结果为依据。不要编造文件、命令、网络请求、验证或完成状态。工具失败或被拒绝时，区分具体原因，并在权限范围内尝试相关替代路径；不得绕过用户拒绝、审批策略或 Sandbox。

涉及当前事实、版本、排名或明确研究请求时，使用可用的检索能力并给出来源。修改代码或工作对象时，先读取相关事实，保持无关内容不变，并运行与改动风险相称的验证。生成物应写入请求指定的位置；未指定路径的报告、导出或文档写入 outputs/。

持续推进已获授权且可安全完成的工作。只有缺少的信息会阻止继续时才请求澄清。面向用户的说明应清楚、简洁，给出结果、关键依据、验证和仍存在的实际限制；不要输出私有思维链或内部重试细节。"""

CONTROL_POLICY = """所有面向用户的最终回答、公开进度、可见推理和工具调用前说明，都使用用户当前请求的主要语言；代码、命令、路径、标识符和原始工具输出保持原样。不要为了展示过程而翻译或泄露私有思维链。
只有复杂、需要持续跟踪或验收的任务才调用 set_plan 建立计划；计划一旦建立，完成前必须用 update_step 更新每个步骤，并用 link_evidence 为需要验证的步骤关联真实证据。普通工具进度由运行时事件呈现，无需反复调用 report_progress。
已知参数且彼此独立的只读调用可以在同一轮发起；写入、执行、审批、依赖前序结果或副作用未知的调用必须保持有序。缺少必要信息且无法安全继续时，单独调用 request_clarification，之后等待用户回复。"""


def _revision(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _source(origin: str, role: Literal["system", "policy"], text: str) -> InstructionSource:
    clean = text.strip()
    return InstructionSource(origin=origin, role=role, text=clean, revision=_revision(clean))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_prompt_profile(
    config: PromptConfiguration,
    *,
    visible_tools: set[str],
    agents_enabled: bool,
    agent_autonomy: str,
) -> PromptProfile:
    """Resolve one immutable prompt profile from config and visible capabilities."""

    sources: list[InstructionSource] = []
    preset: str | None = config.preset if config.mode != "replace" else None
    if config.mode == "minimal":
        sources.append(_source("builtin:minimal", "system", MINIMAL_INSTRUCTIONS))
    elif config.mode in {"preset", "append"}:
        sources.append(_source(f"builtin:{config.preset}", "system", PRESET_INSTRUCTIONS))
    else:
        assert config.replace_file is not None
        sources.append(_source(str(config.replace_file), "system", _read(config.replace_file)))

    sources.append(_source("runtime:control-policy", "policy", CONTROL_POLICY))

    guidance: list[str] = []
    if "search_tools" in visible_tools:
        guidance.append("声明某项扩展能力不可用前，先用 search_tools 查询延迟加载的工具。")
    if "load_skill" in visible_tools:
        guidance.append(
            "available_skills 是当前可用 Skill 目录；任务与说明匹配时，用 load_skill 按名称加载。"
        )
    if {"open_work_product", "apply_work_product_change"}.intersection(visible_tools):
        guidance.append(
            "多轮修改已有交付物或结构化文件时，将其作为工作对象打开并使用受约束的局部修改。"
        )
    if agents_enabled and "spawn_agent" in visible_tools:
        if agent_autonomy == "explicit":
            guidance.append(
                "只有用户、项目指令或已激活 Skill 明确要求委派时才创建子 Agent。"
            )
        else:
            guidance.append(
                "独立子任务能显著提高速度、隔离上下文或增强验证时可创建子 Agent；参数已知的独立任务应在同一轮发起，写入任务不得重叠。"
            )
        guidance.append("完成根任务前，等待并整合所有已请求子 Agent 的结果。")
    elif not agents_enabled or agent_autonomy == "disabled":
        guidance.append("多 Agent 已禁用，请在当前 Agent 内完成任务。")
    if guidance:
        sources.append(_source("runtime:capability-guidance", "policy", "\n".join(guidance)))

    if config.mode == "append":
        assert config.append_file is not None
        custom = _read(config.append_file)
        sources.append(
            _source(
                str(config.append_file),
                "policy",
                f"以下是当前项目指令：\n\n{custom}",
            )
        )
    return PromptProfile(
        mode=config.mode,
        preset=preset,
        version=PROMPT_PRESET_VERSION,
        sources=tuple(sources),
    )


__all__ = [
    "CONTROL_POLICY",
    "MINIMAL_INSTRUCTIONS",
    "PRESET_INSTRUCTIONS",
    "PROMPT_PRESET_VERSION",
    "InstructionSource",
    "PromptProfile",
    "build_prompt_profile",
]
