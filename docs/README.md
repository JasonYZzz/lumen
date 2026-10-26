# Lumen 文档地图

本目录按“当前事实”和“时间点证据”分层。阅读或修改架构时，优先级始终是源码、契约测试与当前
schema，其次是 Accepted 决策记录；计划和研究不能反向定义运行行为。

最近本地质量复核：2026-10-23；源码版本 `2.0.0`，尚未发布。此前文档整理基线为 `28d1679`。
“更新文档”不代表发布新版本或重新完成真实 Provider、跨平台、媒体及 Keychain 验收。

## 当前事实

| 范围 | 当前实现与入口 |
| --- | --- |
| 客户端 | Textual TUI、Next.js Web、headless CLI、Tauri 2 Apple Silicon macOS Beta，共用 WorkspaceHost |
| 持久化 | Session schema v12，兼容读取 v1–v11；配置 schema v2，v1 只在内存迁移 |
| 运行时 | LumenAgentLoop 是唯一主 Loop；完整批次 checkpoint、显式损坏尾行维修、文件恢复预览／确认 |
| 性能与生命周期 | 身份绑定的增量 Session 投影、按字节有界的事件内存尾部及临时磁盘回放；磁盘容量和冷读取仍有约束 |
| 模型配置 | Runtime Provider Catalog 定义已核对能力；桌面模型预设只提供首次连接配置，不携带凭据 |
| Web 连接 | 一次性链接登录；CLI 按工作区／地址持久化私有密钥，刷新／重启保留认证；401 独立恢复、不重发任务 |
| 个人助理与持续跟进 | 目标／关联对话／成果、资料集合与版本、全局活动；单机 owner、文件快照／受限检索、试跑／启用和站内结果 |
| 桌面交付 | arm64 app/DMG、ad-hoc 签名、人工升级；声明最低 macOS 14，最低版本和正式发行门禁待实机验收 |
| 依赖与质量 | Python 3.11–3.13；桌面冻结 Python 3.13；CI Node 22 / pnpm 10，精确依赖以锁文件为准 |

- [`architecture-guide/`](architecture-guide/README.md)：当前架构、运行流程、状态权威与 Accepted 决策。
  网页版入口是 [`architecture-guide/index.html`](architecture-guide/index.html)。
- [长任务持续执行与排障](architecture-guide/14-long-running-recovery.md)：超时、重试、同轮压缩和恢复的当前契约；维护记忆同时保存在根 `AGENTS.md`。
- [v2 个人助理与自动化](architecture-guide/16-personal-assistant-automations.md)：当前功能、状态权威、来源限制、升级回退和未完成的发布门禁。
- [v2 当前实施验证](research/2026-10-21-v2-product-flow-validation.md)：原四项审查修复、目标／资料联动、真实模型、浏览器和本地门禁；原反例保留在[历史审查](research/2026-10-20-v2-code-review.md)。
- [2026-10-23 质量扫描与修复](research/2026-10-23-quality-audit.md)：自动化故障清理、TUI 历史分页、偏好保存与文档新鲜度，含本次完整本地验证及未验收范围。
- [`brand/`](brand/README.md)：Lumen Fold 标志、横向组合、色彩与生产使用约束。
- [`commands.md`](commands.md)：CLI、TUI 与 Web 可见命令行为。
- [`desktop.md`](desktop.md)：原生启动、首次配置、凭据、退出、arm64 构建与发行限制。
- 根目录 [PRODUCT.md](../PRODUCT.md) 与 [DESIGN.md](../DESIGN.md)：产品约束与 Atlas 视觉系统；后者不定义 Web／桌面视觉规范。
- [`skills/authoring.md`](skills/authoring.md)：Skill 作者契约。
- [`hooks/README.md`](hooks/README.md)：Hook 接入与安全约束。
- [`generated/contracts.json`](generated/contracts.json)：由源码生成的契约目录；不能手工修改。
- [`generated/provider-reasoning.md`](generated/provider-reasoning.md)：Provider 推理和原生搜索能力目录。

## 时间点证据

- [2026-10-21 产品研究与联动审计](research/2026-10-21-muse-chatgpt-space-product-research.md)及[优化实施计划](plans/2026-10-21-lumen-v2-product-flow-upgrade.md)：保留升级前证据、设计意图与分阶段实施状态；当前契约以上方架构说明和验证为准。
- [`plans/`](plans/README.md)：仍有未完成发布验证或条件式后续工作的活动计划。计划不等于已实现。
- [`research/`](research/README.md)：固定日期、版本或 commit 的外部研究快照。结论可能被后续实现取代。
- [`spikes/`](spikes/langgraph-context-resume.md)：隔离技术验证，不属于生产依赖。
- 阶段交付按主题合并；已被替代的图、原型和外部页面不再保留并列入口。
  删除依据与迁入的验收门槛见[当前实现审计](architecture-guide/12-current-implementation-audit.md#129-文档合并与清理2026-10-10)。

最近本地检查见[2026-10-23 质量扫描与修复](research/2026-10-23-quality-audit.md)；最初 v2 升级证据见[源码升级与验收记录](research/2026-10-20-v2-implementation-validation.md)，各记录保留其时间点边界。
此前实现及验收记录：运行时提交 `7f5e5ce`、Web 提交 `fa0aba3`、桌面提交 `28d1679`；
见[运行时验收](research/2026-10-09-runtime-optimization-validation.md)与[桌面验收](research/2026-10-09-macos-desktop-validation.md)。
这些记录的测试数量、环境和外部资料日期均属于当次验证，不能外推为新的 CI 或实机验收结果。

## 维护规则

1. 公共行为变化必须同时更新源码、契约测试以及受影响的当前文档。
2. Accepted 决策被替代时新增或更新决策记录，并明确旧决策的状态；不要让两套文档同时声称权威。
3. 研究必须保留日期和证据版本，不改写成“当前实现”。有未完成门禁的计划继续保留；被同一权威
   完全替代的旧计划在迁移仍有效的验证要求后删除，同时更新索引和引用。
4. 无用文件只有在确认零生产引用、被同一权威完全替代或目录已不再需要占位后才删除；独立的
   历史证据和兼容契约不因日期较早而失效。删除依据记录在当前实现审计中。
5. Architecture Atlas 的 Trace Reader 只索引当前事实、源码、测试和生成契约，不索引 research、plans、
   spikes。

刷新并验证网页版证据快照：

```bash
uv run python scripts/build_architecture_atlas.py
uv run python scripts/build_architecture_atlas.py --check
```

## 整理验证（2026-10-10）

本轮只修改 Markdown、删除旧展示附件，并按正式命令刷新 Atlas。
73 份 Markdown/HTML 本地链接目标检查通过；29 份研究和 5 份活动计划完整进入索引。
contracts、Provider catalog 与 Atlas 新鲜度检查通过；Atlas 的 394 条路径/内容摘要、
载荷 JavaScript、架构 SVG 和 diff whitespace 检查通过。

上一轮文档刷新曾运行 Web 231 tests、typecheck/build、Ruff、Pyright 与隔离 wheel 检查；
本轮不将其计数当成重新执行。完整原始验收见上方运行时和桌面报告。
纯文档整理未重跑 Python/Web 全量测试、应用打包或真实 Provider/设备验证。
