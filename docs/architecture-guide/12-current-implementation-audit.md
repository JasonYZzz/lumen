# 12. 当前实现审计

> 审计日期：2026-08-19。本文是源码与契约测试的横截面，不是新的架构决策；Accepted 决策仍以 10、11 章为准。

## 12.1 审计方法

本轮按“运行时权威 → 持久化事实 → 客户端投影 → 契约测试”四个方向交叉核对。源码和测试优先于 README、历史计划与研究文档；框架反射入口不因文本引用少而判定为死代码。

重点证据：

- 应用命令与事件：`src/lumen/application/host.py`、`models.py`、`events.py`；
- turn 与恢复：`run_coordinator.py`、`sessions.py`；
- 模型循环：`runtime.py`、`task_control.py`、`timeline.py`；
- 上下文：`context/engine.py`、`assembler.py`、`compaction.py`；
- mutation 与完成门禁：`work_products/workspace.py`；
- 多 Agent：`agents/orchestrator.py`、`agents/runtime_factory.py`；
- Realtime：`live/manager.py`、`router.py`、`protocol.py`、`tools/gateway.py`；
- 工具契约与展示：`tools/spec.py`、`tools/presentation.py`、`tools/registry.py`、`tools/gateway.py`；
- 生命周期与观测：`lifecycle.py`、`resources.py`、`contracts.py`；
- 离线 Atlas：`scripts/build_architecture_atlas.py`、`content.generated.js`、`document-inspector.js`；
- 安全：`tools/workspace.py`、`tools/web.py`、`sandbox.py`、`trust.py`；
- 契约：`tests/test_workspace_host.py`、`test_sessions.py`、`test_work_products.py`、`test_agent_orchestrator.py`、`test_live*.py`、`test_tool_presentation.py`、`test_registration_scope.py`、`test_contract_catalog.py`、`test_web_api.py`。

## 12.2 状态权威矩阵

| 状态概念 | 唯一运行时权威 | 持久化权威 | Adapter / 投影 |
|---|---|---|---|
| workspace command、active run、审批 future | `WorkspaceHost` | run timeline 与 session records | TUI、FastAPI/SSE、Web |
| 单 Session turn、active history、恢复游标 | `RunCoordinator` | `SessionRepository` JSONL | Host actor |
| 单 Agent 模型/工具 loop | `AgentRuntime` | turn messages、events、partial outcome | provider/Pydantic AI |
| 上下文候选与 checkpoint 发布 | `ContextEngine` | turn compaction + ArtifactStore | Context assembler、memory repository |
| Work Product、effect、验证 | `TaskWorkspace` | `work_state` / `effect` records | 工具与 Host 命令 |
| Agent Thread 生命周期 | `AgentOrchestrator` | v8+ Agent records | 模型工具、LegacyChildRunAdapter、Host |
| Live call 生命周期 | `LiveSessionManager` | v9 `live_session` records + live turns | Provider Adapter、Web media client |
| 长期记忆 | `MemoryManager` + SQLite repository | `memory.sqlite3` | Markdown audit projection |
| 可逆注册与后台 task | `RegistrationScope` | 无；仅保留有界关闭 diagnostic | ResourceManager runtime/resource scopes |
| 工具真实结果 | Tool callable + `ToolOutputSpec` canonical value | tool result / effect records | 模型文本、`ToolCallView/ToolResultView` |

结论：当前核心状态没有第二套生产权威。`SessionRepository` 只追加事实，不调度；UI reducer、TimelineStore、Legacy Child API 和 Web voice client 都是投影或 Adapter。

## 12.3 深 Module 评估

### 高 Depth

- `ContextEngine.prepare/commit/control`：小 Interface 隐藏 profile、token counter、Zone、压缩、checkpoint、memory 与两阶段发布，删除后复杂度会散落到 runtime、session 和客户端。
- `AgentRuntime.run`：调用者只提供输入、history、审批与事件 sink，即可获得 provider streaming、工具循环、recovery receipt、clarification 与 completion gate。
- `TaskWorkspace`：把 target 选择、snapshot、局部变更验证、effect journal、reconcile 和 completion issues 收束在同一 Module。
- `LiveSessionManager` + `RealtimeProviderAdapter`：canonical control 与厂商 wire protocol 分离；两个生产 Adapter 证明该 Seam 是真实替换点。
- `ToolSpec` + `CapabilityGateway`：一个小契约同时收束参数、canonical 输出、模型投影、客户端投影、Risk、EffectKind、ToolConcurrency、guard 与幂等；删除后这些规则会重新散落到文字和语音 transport。

### 需要持续控制 Interface 面积

- `WorkspaceHost` 的 Depth 仍成立，但 command union 已覆盖 session、run、context、Agent、Work Product 与 Live。新增客户端行为应扩展现有 command/event，不把内部对象暴露给 UI；同时应避免把纯领域规则继续堆入 `dispatch`。
- `SessionRepository` 的 append 方法随 schema 增多。其价值来自 append-only 校验、兼容读取与 ownership 检查，而不是方法数量；后续 record 类型增加时应优先保持类型化 append/load 契约，不引入通用“写任意 record”逃生口。

## 12.4 已确认的实现演进

1. **Session 已是 v9。** 新增 `live_session` record；v7 Work Product、v8 Agent、v9 Live 都通过追加 `schema_upgrade` 支持历史会话，不重写 header。
2. **Sandbox 已进入生产路径。** 默认 `workspace_write` 使用 Seatbelt/bubblewrap 且 fail closed；旧文档“仅实现审批和路径限制”已失效。
3. **审批增加项目永久范围。** `always` 规则由 `ApprovalRuleStore` 以项目 identity 持久化，不污染配置 YAML。
4. **Provider reasoning 可展示。** `ThinkingDelta` 与最终文本、commentary 分离，只作为 timeline 展示，不进入 completion 文本。
5. **Web 能力进入统一 Tool Registry。** `web_fetch` 默认注册；`web_search` 按 provider 配置注册；二者对 child runtime 只在父级有效工具交集中保留。
6. **child progress 可审计但有界。** 非控制工具与 `report_progress` 被投影为 `agent.progress`，每 Agent 上限 60 条，避免 Session journal 被展示性事件淹没。
7. **Tool Contract 已进入 V2。** canonical JSON value、模型文本和客户端展示意图分离；presentation 失败回退，不改写 authoritative outcome；并发从 EffectKind 拆为 per-invocation `ToolConcurrency`。
8. **provider 请求已有 durable receipt。** 每个实际模型步骤保存 route、token 分区、visible tool digest 和 Context fingerprint；失败与取消不会丢失已经发出的请求证据。
9. **注册生命周期可逆。** `RegistrationScope` 以 LIFO disposer 和 task quiescence 保证重开、切换模型不会累积工具、MCP schema 或 listener；它明确不拥有领域状态。
10. **文件 mutation 使用 expected revision。** 新建 no-replace、替换前重验、符号链接 hop 拒绝并统一返回 `STALE_RESOURCE`；Work Product 与 legacy 文件工具共用同一 Workspace seam。
11. **能力与配置可解释但不双轨。** `capabilities_report()` 和 config resolution report 为 CLI/TUI/Web 提供脱敏只读投影，运行时仍以 Registry/Policy 与 `AppConfig` 为权威。
12. **契约目录由源码生成。** catalog v2 覆盖配置、命令、事件、工具、Session 与关键不变量；CI 的 `python -m lumen.contracts --check` 阻止生成物漂移。
13. **Architecture Atlas 不再复制第二套正文。** `DocumentInspector` 用一个 `open(path)` Interface 统一 Markdown、原文、源码、搜索和阅读历史；确定性 `content.generated.js` 只保存可重建证据快照，CI 的 `scripts/build_architecture_atlas.py --check` 在相关文档、源码、测试或生成契约变化时阻止旧 Web 内容通过。

## 12.5 兼容、安全与恢复结论

- 保留 v1–v8 Session 只读兼容；升级只追加，不原地迁移。
- 配置仍为 v2；v1 仅内存迁移并警告。
- `Risk`、`EffectKind` 与 `ToolConcurrency` 保持三轴独立；未知远端动作不自动重放，未声明并发安全时使用 exclusive。
- `ToolGuard` 只有 abstain/deny；任一 deny 都不能被后续 Hook 或 Adapter 放宽。
- presentation、能力清单、配置报告、Session projection cache 与 contract catalog 都是可重建投影，不可反向成为状态权威。
- Agent role 只收窄父级工具、审批与 sandbox；child 无控制工具，最大深度一。
- fork 不复制运行中执行；active/pending Agent 在新 Session 标为 `not_carried`。
- Live 重启不恢复 Provider call；存在 pending 动作时进入 `reconciliation_required`。
- completion 同时受 Plan evidence、TaskWorkspace verification 和 AgentOrchestrator unresolved state 阻止。

## 12.6 文档维护规则

每次改变以下任一契约，应在同一修改中更新本指南与离线 Atlas：Session schema、`WorkspaceCommand`、`RunEvent`、`Risk`/`EffectKind`/`ToolConcurrency`、`ToolOutputSpec`/presentation、Context Zone/Provider receipt、Agent status、Live canonical Interface、默认 sandbox 与 mutation 行为。生成的 contract catalog、OpenAPI、TypeScript schema 与 `content.generated.js` 只能通过对应命令刷新，不能手工编辑；提交前分别运行 `python -m lumen.contracts --check` 与 `python scripts/build_architecture_atlas.py --check`。
