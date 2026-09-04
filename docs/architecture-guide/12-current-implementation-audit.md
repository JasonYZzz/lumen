# 12. 当前实现审计

> 审计日期：2026-09-04。本文是源码与契约测试的横截面，不是新的架构决策；Accepted 决策以 10、11、13 章为准。

## 12.1 审计方法

本轮按“运行时权威 → 持久化事实 → 客户端投影 → 契约测试”四个方向交叉核对。源码和测试优先于 README、历史计划与研究文档；框架反射入口不因文本引用少而判定为死代码。

重点证据：

- 应用命令与事件：`src/lumen/application/host.py`、`models.py`、`events.py`；
- turn 与恢复：`run_coordinator.py`、`sessions.py`；
- 模型循环：`runtime.py`、`agent_loop/driver.py`、`agent_loop/loop.py`、
  `agent_loop/pydantic_driver.py`、`task_control.py`、`timeline.py`；
- 上下文：`context/engine.py`、`assembler.py`、`compaction.py`；
- mutation 与完成门禁：`work_products/workspace.py`；
- 多 Agent：`agents/orchestrator.py`、`agents/runtime_factory.py`；
- Realtime：`live/manager.py`、`router.py`、`protocol.py`、`tools/gateway.py`；
- 工具契约与展示：`tools/spec.py`、`tools/presentation.py`、`tools/registry.py`、`tools/gateway.py`；
- 生命周期与观测：`lifecycle.py`、`resources.py`、`contracts.py`；
- 离线 Atlas：`scripts/build_architecture_atlas.py`、`content.generated.js`、`document-inspector.js`；
- 安全：`tools/workspace.py`、`tools/web.py`、`sandbox.py`、`trust.py`；
- 契约：`tests/test_workspace_host.py`、`test_sessions.py`、`test_runtime.py`、`test_lumen_agent_loop.py`、
  `test_capability_gateway.py`、`test_delegation.py`、`test_timeline.py`、`test_resources.py`、
  `test_web_api.py` 与 Web reducer tests；contract catalog 由 `python -m lumen.contracts --check` 校验。

## 12.2 状态权威矩阵

| 状态概念 | 唯一运行时权威 | 持久化权威 | Adapter / 投影 |
|---|---|---|---|
| workspace command、active run、审批 future | `WorkspaceHost` | run timeline 与 session records | TUI、FastAPI/SSE、Web |
| 单 Session turn、active history、恢复游标 | `RunCoordinator` | `SessionRepository` JSONL | Host actor |
| 单 Agent turn 外层与公开执行契约 | `AgentRuntime` | turn messages、events、partial outcome | `LumenAgentLoop` Adapter |
| 模型—工具状态机（唯一权威） | `LumenAgentLoop` | 通过 `AgentRuntime` 投影到同一 turn | `ModelDriver`、`CapabilityGateway` |
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

- `ContextEngine.prepare/prepare_step/commit/control`：小 Interface 隐藏 profile、token counter、Zone、同轮压缩、checkpoint、memory 与两阶段发布，删除后复杂度会散落到 runtime、session 和客户端。
- `AgentRuntime.run`：调用者只提供输入、history、审批与事件 sink，即可获得 provider streaming、工具循环、recovery receipt、clarification 与 completion gate；内部不存在 engine selector。
- `LumenAgentLoop.run`：显式状态机收束 provider event 顺序、工具批次、请求预算、交互续接与 terminal candidate；删除后这些规则会重新散落到 Runtime 与 Driver。
- `TaskWorkspace`：把 target 选择、snapshot、局部变更验证、effect journal、reconcile 和 completion issues 收束在同一 Module。
- `LiveSessionManager` + `RealtimeProviderAdapter`：canonical control 与厂商 wire protocol 分离；两个生产 Adapter 证明该 Seam 是真实替换点。
- `ToolSpec` + `CapabilityGateway`：一个小契约同时收束参数、canonical 输出、模型投影、客户端投影、Risk、EffectKind、ToolConcurrency、guard 与幂等；删除后这些规则会重新散落到文字和语音 transport。

### 需要持续控制 Interface 面积

- `WorkspaceHost` 的 Depth 仍成立，但 command union 已覆盖 session、run、context、Agent、Work Product 与 Live。新增客户端行为应扩展现有 command/event，不把内部对象暴露给 UI；同时应避免把纯领域规则继续堆入 `dispatch`。
- `SessionRepository` 的 append 方法随 schema 增多。其价值来自 append-only 校验、兼容读取与 ownership 检查，而不是方法数量；后续 record 类型增加时应优先保持类型化 append/load 契约，不引入通用“写任意 record”逃生口。

## 12.4 已确认的实现演进

1. **Session 已是 v9。** v5 Context、v6 Settings/Plan、v7 Work Product、v8 Agent、v9 Live 事实都通过追加 `schema_upgrade` 支持历史会话；marker 必须形成连续的有效 schema 链，且不重写 header。
2. **Sandbox 已进入生产路径。** 默认 `workspace_write` 使用 Seatbelt/bubblewrap 且 fail closed；旧文档“仅实现审批和路径限制”已失效。
3. **审批增加项目永久范围。** `always` 规则由 `ApprovalRuleStore` 以项目 identity 持久化，不污染配置 YAML。
4. **Provider reasoning 可展示。** `ThinkingDelta` 与最终文本、commentary 分离，只作为 timeline 展示，不进入 completion 文本。
5. **Web 能力进入统一 Tool Registry。** `web_fetch` 默认注册；`web_search` 按 provider 配置注册；二者对 child runtime 只在父级有效工具交集中保留。
6. **child progress 可审计但有界。** 非控制工具与 `report_progress` 被投影为 `agent.progress`，每 Agent 上限 60 条，避免 Session journal 被展示性事件淹没。
7. **Tool Contract 已进入 V2。** canonical JSON value、模型文本和客户端展示意图分离；presentation 失败回退，不改写 authoritative outcome；并发从 EffectKind 拆为 per-invocation `ToolConcurrency`。
8. **provider 请求已有 durable receipt 与 input manifest。** 每个实际模型步骤保存 route、token 分区、完整有序 tool schema digest、Context fingerprint、source refs/digests、stable prefix、dynamic tail、request fingerprint 和 replay eligibility；失败与取消不会丢失已经发出的请求证据，Manifest 不复制正文或 canonical history。
9. **注册生命周期可逆。** `RegistrationScope` 以 LIFO disposer 和 task quiescence 保证重开、切换模型不会累积工具、MCP schema 或 listener；它明确不拥有领域状态。
10. **文件 mutation 使用 expected revision。** 新建 no-replace、替换前重验、符号链接 hop 拒绝并统一返回 `STALE_RESOURCE`；Work Product 与 legacy 文件工具共用同一 Workspace seam。
11. **能力与配置可解释但不双轨。** `capabilities_report()` 和 config resolution report 为 CLI/TUI/Web 提供脱敏只读投影，运行时仍以 Registry/Policy 与 `AppConfig` 为权威。
12. **契约目录由源码生成。** catalog v7 覆盖配置、命令、事件、工具、Session、Model Input
    Manifest、`LumenAgentLoop` 状态/事件与关键不变量；CI 的
    `python -m lumen.contracts --check` 阻止生成物漂移。
13. **Architecture Atlas 不再复制第二套正文。** `DocumentInspector` 用一个 `open(path)` Interface 统一 Markdown、原文、源码、搜索和阅读历史；确定性 `content.generated.js` 只保存当前文档、源码、测试和生成契约的可重建证据快照，研究、计划、归档与 spike 不混入当前事实检索。CI 的 `scripts/build_architecture_atlas.py --check` 在相关证据变化时阻止旧 Web 内容通过。
14. **TUI capability mutation 已收口到 Host。** Skill、MCP Prompt/Resource、Hook 与 Context control
    不再直接操作 `ResourceManager` / `ContextEngine`；Web 与 TUI 共享同一 command/result Seam。
15. **用户图片输入使用 ArtifactRef。** Web 上传、TUI 图片路径、交互队列和普通 run 共享
    `AttachmentRef`；Runtime 仅在 Provider boundary 解析字节，并在持久化前恢复为引用 marker。
    Session v9 turn 的可选 `attachments` 字段保存引用和有界 metadata，不保存 Base64；缺少显式
    `input_modalities: [text, image]` 时在 Provider I/O 前安全失败。
16. **LumenAgentLoop 已成为单一权威。** `AgentRuntime` 只驱动 provider-neutral `ModelDriver` 与
    `LumenAgentLoop`；主模型—工具路径的 PydanticAI `Agent` graph、selector、output validator、
    capability wrapper 和私有 toolset 修改已删除。无工具的 Context 摘要/Memory 提取可继续使用 typed
    structured-output Adapter，但不拥有 turn 调度。低层 `PydanticAIModelDriver` 保留 Provider Adapter
    价值，并提供 exact/partial response、生命周期、suspended continuation、usage/cache 与
    provider-private part 保真。
17. **Deferred MCP 与 child Runtime 已进入同一能力权威。** run-local `search_tools` 只改变下一请求的
    可见 schema，不创建第二个 Registry；child 使用独立 Context/Driver 与收窄 Gateway，writable
    capability 重绑定 worktree，MCP 只复用连接 Adapter。
18. **本地执行已是跨进程单 writer。** `WorkspaceHost` 的状态锁仍拥有进程内 active run，轻量
    `WorkspaceRunLock` 在文字 Run、Live 工具执行或目录可见性修改期间持有 OS advisory lock；第二个 Host 可读但不能
    并发执行，取得锁后会从 Session journal 刷新 actor，进程退出由 OS 自动释放锁。
19. **中断 retry 与诊断不增加状态权威。** orphaned `running` turn 只投影为 `interrupted`；retry 恢复
    AttachmentRef，并在 unresolved/unknown Effect 存在时 fail closed。`latest_run` 诊断直接投影已有
    receipts、usage、timeline 和 diagnostics，省略所有正文与未知字段，不新增 telemetry store。
20. **Realtime 的隔离 contract coverage 仍不完整。** 当前 Python 覆盖集中在 `test_web_api.py` 的 Host
    canonical control、PCM、认证与 SSE，Web reducer 另有 Vitest；仓库没有旧文档所列的独立 Router、
    OpenAI Adapter 或 Bailian wire tests，因此这部分是测试债务，不能描述为已具备完整 Provider 覆盖。
21. **MCP 发现与命令失败必须按实际 Interface 解释。** `search_tools` 的 Provider-visible description
    包含有界目录，支持 Unicode 与无关键词分页浏览；发现不改变权限。SandboxRunner 的 network
    只约束子进程，公共 TLS/运行时依赖读取已修复；MCP 启动状态单独披露。断线自动重试只限配置
    明确声明 observe 的工具，read 风险不代替 effect 声明。
22. **并行与 Web 大响应存在明确实现限制。** 原生 Loop 的两个 parallel 模式都保留 exclusive
    屏障，只有显式 PARALLEL_SAFE 的调用可重叠。内置 Web 和 MCP Gateway 当前默认 exclusive；
    Web fetch 的字节上限只限制解析内容，HTTP 响应仍先完整读入，尚无传输级流式大小限制。
23. **外部结果的恢复责任已进入运行契约。** strict unknown 工具在 pre-invoke 阶段拒绝，成功
    observe 不产生 effect；非自记录的远端动作在 dispatch 前写 prepared，异常/取消保留对账状态。
    CompletionBlocker 区分模型修复与人工恢复，后者不会触发盲目完成重试。Host 在新运行与编辑
    分支受理前检查外部对账，Web 提供按单条操作填写依据的恢复入口；历史 journal 不自动豁免。

24. **任务可见性与完成验收分离。** 历史 unresolved effect 不阻止归档或软删除；Host 在 OS lock
    内检查活动执行、所有 root run 的活动 Agent 和 Live 连接，不借用 completion gate。
    删除保留 journal 与证据，重复删除幂等；缓存 actor 先验证墓碑，Session 投影保持删除终态，
    防止另一个进程晚到的标题快照把任务复活。继续执行与完成声明仍要求处理未知外部结果。

25. **长任务恢复不再混淆时间与次数。** 请求/工具预算和模型总时限默认 `null`，300 秒空闲随数据
    延续。原生 Loop 统一重试和尝试计数，SDK 不叠加 OpenAI/Anthropic 主模型重试。
26. **可见候选可撤回、完整工具批次可恢复。** 自动重试不重做前面工具；失败后 Session load 也恢复
    完整批次。Web/TUI/Timeline 按 Unicode 字符跨 thinking 段撤回。未知外部动作仍不能自动重放。
27. **同轮压缩已有持久化闭环。** prepare_step 可在一个 run 内多次总结，最终 checkpoint 以持久父
    节点为依据，Session 从 source_end 推导已覆盖前缀，避免重载重复消息；摘要与写盘失败不推进游标。

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
- 图片限于 PNG/JPEG/GIF/WebP、单张 20 MiB、每个输入最多 8 张；Artifact 内容、声明 MIME、
  magic signature 和 byte size 必须一致。Responses Adapter 映射为 `input_image`，显式 Chat
  compatibility Adapter 映射为 `image_url`。

## 12.6 文档维护规则

每次改变以下任一契约，应在同一修改中更新本指南与离线 Atlas：Session schema、`WorkspaceCommand`、`RunEvent`、`Risk`/`EffectKind`/`ToolConcurrency`、`ToolOutputSpec`/presentation、Context Zone/Provider receipt、Agent status、Live canonical Interface、默认 sandbox 与 mutation 行为。生成的 contract catalog、OpenAPI、TypeScript schema 与 `content.generated.js` 只能通过对应命令刷新，不能手工编辑；提交前分别运行 `python -m lumen.contracts --check` 与 `python scripts/build_architecture_atlas.py --check`。历史计划不得反向覆盖 Accepted 决策或源码事实；已被同一权威完全替代的计划，在迁移有效门禁、清理引用并记录证据后删除。

## 12.7 过期材料清理记录（2026-09-03）

| 删除内容 | 可删除的证据 | 保留的权威或验证 |
|---|---|---|
| 2026-07-27 M6–M9 路线图 | 以旧 SDK 调度、按 READ 风险并行和早期阶段为基线；当前实现已由原生 Loop、Gateway、Orchestrator 接管 | 本章及第 4、9、10、13 章；`test_orchestration_m6_m9.py` 中仍有效的 Hook/MCP/审批契约保留 |
| 2026-08-26 Loop 迁移计划 | 单轨迁移完成，Module 权威、删除依据与恢复约束已由 Accepted 第 13 章替代 | 有效发布门禁迁移至 13.9；真实 Provider 性能基线仍由活动计划跟踪 |
| 两份 archive 索引 | 两份被索引的旧计划已删除，目录不再承载独立事实 | 文档地图与活动计划索引 |
| `test_project_config_uses_expected_model_registry` | 依赖本机私有 `agent.yaml` 的固定模型名单；不是发行包或配置 schema 契约 | `test_repo_example_config_loads_without_drift`、临时配置 fixture、多模型切换及 resolver 测试 |
| `test_inline_approval_replaces_modal` | 只断言已删除 Module/类不存在，无实际交互验证；当前 App 使用 ApprovalPanel | `test_approval_panel.py`、TUI 实际审批交互和 snapshot |
| `test_select_model_failure_restores_old_runtime_not_none` | 描述的是已替代的 detach/restore 过程；非空断言被同文件更强的旧实例 identity 与模型名断言完全覆盖 | `test_select_model_failure_preserves_old_model_and_runtime` 及 candidate 发布前可见性测试 |
| `tests/test_runtime.py` 的 PlanStepInput 伪重导出 | 仓库无消费者，只由 `_ = PlanStepInput` 保活 | 正式领域类型与 `test_task_control.py` 的公开契约 |

没有按日期或 `legacy` 名称批量删除测试。ContextManager 仍是 ContextEngine 的内部结构化摘要
Implementation；Session v1–v8、配置 v1、旧 child Adapter 仍是兼容契约。Web Mascot 仍由
`LumenApp → LandingEntry → MascotScene` 调用，相关测试与待交付 Rive 计划保留。外部研究与隔离
spike 保留其日期、版本和证据属性，不将它们描述为当前生产行为。

本次清理验证：全量 `uv run pytest` 通过 941 项测试和 60 个 snapshot；Ruff、Pyright、
contract catalog 检查通过。45 份 Markdown 的相对文件链接及 Atlas 的 8 个显式源码入口
无断链，Atlas 生成物已刷新并通过一致性检查。本次未修改 Web Implementation，未重跑 Web 构建。
