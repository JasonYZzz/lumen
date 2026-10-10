# Lumen 文档地图

本目录按“当前事实”和“时间点证据”分层。阅读或修改架构时，优先级始终是源码、契约测试与当前
schema，其次是 Accepted 决策记录；计划和研究不能反向定义运行行为。

实现复核：2026-10-10；本轮文档核对的代码基线为 `28d1679`，项目版本保持 `0.1.0`。
“更新文档”不代表发布新版本或重新完成真实 Provider、跨平台、媒体及 Keychain 验收。

## 当前事实

| 范围 | 当前实现与入口 |
| --- | --- |
| 客户端 | Textual TUI、Next.js Web、headless CLI、Tauri 2 Apple Silicon macOS Beta，共用 WorkspaceHost |
| 持久化 | Session schema v12，兼容读取 v1–v11；配置 schema v2，v1 只在内存迁移 |
| 运行时 | LumenAgentLoop 是唯一主 Loop；完整批次 checkpoint、显式损坏尾行维修、文件恢复预览／确认 |
| 性能与生命周期 | 身份绑定的增量 Session 投影、按字节有界的事件内存尾部及临时磁盘回放；磁盘容量和冷读取仍有约束 |
| 模型配置 | Runtime Provider Catalog 定义已核对能力；桌面模型预设只提供首次连接配置，不携带凭据 |
| 桌面交付 | arm64 app/DMG、ad-hoc 签名、人工升级；声明最低 macOS 14，最低版本和正式发行门禁待实机验收 |
| 依赖与质量 | Python 3.11–3.13；桌面冻结 Python 3.13；CI Node 22 / pnpm 10，精确依赖以锁文件为准 |

- [`architecture-guide/`](architecture-guide/README.md)：当前架构、运行流程、状态权威与 Accepted 决策。
  网页版入口是 [`architecture-guide/index.html`](architecture-guide/index.html)。
- [长任务持续执行与排障](architecture-guide/14-long-running-recovery.md)：超时、重试、同轮压缩和恢复的当前契约；维护记忆同时保存在根 `AGENTS.md`。
- [`brand/`](brand/README.md)：Lumen Fold 标志、横向组合、色彩与生产使用约束。
- [`commands.md`](commands.md)：CLI、TUI 与 Web 可见命令行为。
- [`desktop.md`](desktop.md)：原生启动、首次配置、凭据、退出、arm64 构建与发行限制。
- 根目录 [PRODUCT.md](../PRODUCT.md) 与 [DESIGN.md](../DESIGN.md)：产品约束与 Atlas 视觉系统；后者不定义 Web／桌面视觉规范。
- [`skills/authoring.md`](skills/authoring.md)：Skill 作者契约。
- [`hooks/README.md`](hooks/README.md)：Hook 接入与安全约束。
- [`generated/contracts.json`](generated/contracts.json)：由源码生成的契约目录；不能手工修改。
- [`generated/provider-reasoning.md`](generated/provider-reasoning.md)：Provider 推理和原生搜索能力目录。

## 时间点证据

- [`plans/`](plans/README.md)：仍有未完成发布验证或条件式后续工作的活动计划。计划不等于已实现。
- [`research/`](research/README.md)：固定日期、版本或 commit 的外部研究快照。结论可能被后续实现取代。
- [`spikes/`](spikes/langgraph-context-resume.md)：隔离技术验证，不属于生产依赖。
- `diagrams/`、研究 HTML 和 UI prototype 保留各自生成时的设计快照；当前运行拓扑以 Architecture Atlas 和其 SVG 为准。
- [旧架构图](diagrams/lumen-architecture.html)与[计划 UI 原型](chatgpt_codex_plan_ui_prototype.html)保留历史标记；
  对应 `visual-check` 是原图生成时的视觉验证材料，根目录 `pd.html` 是 Pi 外部页面参考，不是 Lumen 技术规范。

最近实现及验收记录：运行时提交 `7f5e5ce`、Web 提交 `fa0aba3`、桌面提交 `28d1679`；
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

## 本轮文档验证（2026-10-10）

- Ruff、strict Pyright、contract catalog、Provider catalog、OpenAPI 与 Atlas 新鲜度检查通过。
- 95 份 Markdown／HTML 的本地链接目标存在；41 份 Markdown 研究记录全部进入研究索引。
- Atlas 的 394 条证据路径与内容摘要一致，桌面 Rust／启动页可读取，研究、构建目录和本地 QA 不进入快照；浏览器载荷 JavaScript 与架构 SVG 的 v12 标签检查通过。
- Web 231 tests、typecheck、生产构建和 `uv build` 通过；wheel 元数据及内置 Web 已检查，隔离安装的 `lumen --version`／`lumen --check-config` 通过。

本轮未修改 Runtime／Host／Session 契约，未重跑 Python 全量测试，也未重做 DMG、真实 Provider、
语音、Keychain、最低 macOS 或跨平台验收；此前报告的原始环境和剩余门禁保持可追溯。
