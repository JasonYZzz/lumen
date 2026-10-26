# 研究资料索引

本目录保留固定日期、版本或 commit 的设计证据与独立验收记录；现行运行行为以源码、
契约测试和 Accepted 决策为准，见[文档地图](../README.md#当前事实)。

整理：2026-10-10。重复阶段交付已合并，完整原文仍可在 Git 历史追溯；
测试数量属于原始验收，不表示此次整理刷新外部资料或重新完成实机测试。

## 实施与验收

- [全仓库质量扫描与修复](2026-10-23-quality-audit.md)：自动化故障清理、TUI 历史分页、偏好保存及文档新鲜度，含最终全量测试与发行边界。
- [Lumen 功能联动与 UI 升级验收](2026-10-22-muse-ui-upgrade-validation.md)：创建并规划、成果版本信息流、目标待处理生命周期、桌面与手机界面升级。
- [v2 目标、资料空间与持续跟进闭环实施验收](2026-10-21-v2-product-flow-validation.md)：含权限与管理、旧数据回收及 Web 刷新认证恢复验证。
- [v2 代码审查：已复现问题与本轮回归](2026-10-20-v2-code-review.md)
- [v2 源码升级、自动验证与浏览器验收](2026-10-20-v2-implementation-validation.md)
- [运行时优化本地验收、性能样本与限制](2026-10-09-runtime-optimization-validation.md)
- [macOS 桌面、模型预设与首次配置验收](2026-10-09-macos-desktop-validation.md)
- [WebUI Three.js 实施与未完成门禁](2026-09-18-threejs-implementation.md)
- [Web 交互演进与验收：过程、消息、标题、Plan、文件预览及设计 QA](2026-09-18-web-conversation-experience.md)
- [同 Session 消息重生成与活动历史审计](2026-09-10-in-place-message-regeneration.md)
- [Provider 推理控制闭环、Loop 基线与实验入口](2026-09-08-reasoning-control-closure.md)
- [报告执行回归调查](2026-09-07-report-execution-regression.md)
- [长任务持续执行：实施与验证记录](2026-09-04-run-resilience-upgrade.md)
- [Skill 安装故障、原生实现与全局范围澄清修复](2026-09-04-skill-installation-history.md)
- [失败任务删除与恢复门禁审计](2026-09-04-session-visibility-recovery-audit.md)
- [Exa 完成失败与 Codex/Pi 源码审计](2026-09-03-completion-recovery-codex-pi-audit.md)
- [Sandbox、MCP 发现与 Harness 失效分析](2026-09-03-sandbox-mcp-harness-audit.md)

## 架构与技术研究

- [Muse 与 ChatGPT 资料空间的功能研究及 Lumen 联动审计](2026-10-21-muse-chatgpt-space-product-research.md)：一级资料核验、当前账号观察、真实 Lumen 截图和优化依据；区别于独立演示原型。
- [Lumen 网页检索与文件下载能力审计](2026-09-10-web-retrieval-capability-audit.md)
- [Thinking 标签、协议与产物输出审校](2026-09-04-thinking-plan-output-review.md)
- [Pi、Claude Code 与 Codex 架构研究](agent-architecture-comparison.md)
- [Codex、DeepSeek Harness 与 Pi 核心研究](agent-harness-research.md)
- [DeepSeek Harness 深度研究](deepseek-harness-architecture-research.md)
- [Lumen、Pi 与 DeepSeek 源码级对比](lumen-pi-deepseek-harness-architecture-comparison.md)
- [上游固定版本证据](upstream-pi-deepseek-harness-sources.md)
- [Lumen Harness 升级分析](lumen-agent-harness-upgrade-analysis.md)
- [外部 Agent 接入的评估与 Proposed 设计](../plans/2026-09-15-external-agent-integration-plan.md)

## Provider 与体验调研

- [国产实时语音 Provider](china-realtime-voice-provider-research.md)
- [OpenAI Realtime API](openai-realtime-api-technical-research.md)
- [OpenAI Responses API Agent](openai-responses-api-agent-research.md)
- [DeepSeek V4 Pro API](deepseek-v4-pro-0813-api-model-research.md)
- [Kimi K3 API](kimi-k3-api-model-research.md)
- [Web/TUI 交互的竞品参照与取舍](2026-09-03-web-tui-interaction-upgrade.md)
- [TUI 体验竞品审计](tui-ux-competitive-audit.md)
- [WebGL/Three.js 升级前调研](2026-09-18-webgl-threejs-opportunities.md)
- [Web 3D 角色技术基准](web-3d-character-benchmark.md)

删除依据与替代入口见[当前实现审计](../architecture-guide/12-current-implementation-audit.md#129-文档合并与清理2026-10-10)。
