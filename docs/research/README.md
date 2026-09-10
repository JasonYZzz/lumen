# 研究资料索引

本目录保存按日期、版本或固定 commit 得出的研究快照，用于解释设计依据，不是当前实现规范。若研究中的
“当前状态”与源码、契约测试或 Accepted 决策冲突，以后者为准。

## Agent Harness 与架构

- [Lumen 网页检索与文件下载能力审计](2026-09-10-web-retrieval-capability-audit.md)
- [同 Session 消息重生成与活动历史审计](2026-09-10-in-place-message-regeneration.md)
- [长任务持续执行：实施与验证记录](2026-09-04-run-resilience-upgrade.md)
- [长任务上限、滑动超时与自动恢复方案](2026-09-04-run-limits-sliding-recovery-analysis.md)
- [全局 Skill 安装与澄清交互修复](2026-09-04-skill-scope-clarification-ui.md)
- [原生 Skill 安装与体验对齐](2026-09-04-native-skill-installation.md)
- [Skill 安装长时间不结束根因](2026-09-04-skill-install-failure-audit.md)
- [Thinking 标签、计划 UI 与产物输出审校](2026-09-04-thinking-plan-output-review.md)
- [失败任务删除与恢复门禁审计](2026-09-04-session-visibility-recovery-audit.md)
- [Exa 完成失败、编辑分支与 Codex / Pi 源码审计](2026-09-03-completion-recovery-codex-pi-audit.md)
- [Sandbox、MCP 发现与 Harness 失效分析](2026-09-03-sandbox-mcp-harness-audit.md)
- [Pi、Claude Code 与 Codex 架构研究](agent-architecture-comparison.md)
- [Codex、DeepSeek Harness 与 Pi 核心研究](agent-harness-research.md)
- [DeepSeek Harness 深度研究](deepseek-harness-architecture-research.md)
- [Lumen、Pi 与 DeepSeek 源码级对比](lumen-pi-deepseek-harness-architecture-comparison.md)
- [上游固定版本证据](upstream-pi-deepseek-harness-sources.md)
- [Lumen Harness 升级分析](lumen-agent-harness-upgrade-analysis.md)
- [Dongbi v7 架构图](dongbi-agent-harness-architecture-diagram-design-v7.html)

## Provider 与交互研究

- [任务进度、侧栏与模型菜单](2026-09-03-conversation-chrome.md)
- [页边滚动与异步标题](2026-09-03-scroll-async-titles.md)
- [消息编辑、停止与文本标签](2026-09-03-message-edit-stop.md)
- [Web 过程展示：ChatGPT 参考审计与实现决策](2026-09-03-process-disclosure.md)
- [Web / TUI 交互升级：ChatGPT、Claude 与 Claude Code 参照](2026-09-03-web-tui-interaction-upgrade.md)
- [国产实时语音 Provider](china-realtime-voice-provider-research.md)
- [OpenAI Realtime API](openai-realtime-api-technical-research.md)
- [OpenAI Responses API Agent](openai-responses-api-agent-research.md)
- [DeepSeek V4 Pro API](deepseek-v4-pro-0813-api-model-research.md)
- [Kimi K3 API](kimi-k3-api-model-research.md)
- [TUI 体验竞品审计](tui-ux-competitive-audit.md)
- [Web 3D 角色技术基准](web-3d-character-benchmark.md)

根目录中的
[`../lumen-pi-deepseek-harness-architecture-comparison.html`](../lumen-pi-deepseek-harness-architecture-comparison.html)
是对应 Markdown 研究快照的独立网页导出，为保持既有相对源码链接和稳定入口暂不移动。
