# 10. 原生多 Agent Runtime 决策记录

状态：Accepted  
日期：2026-08-11

## 决策

Lumen 使用 Host 生命周期内稳定的 `AgentOrchestrator` 作为多 Agent 控制面，以 Session v10 append-only records 作为持久状态权威。模型可调用的 spawn、message、follow-up、wait、interrupt、list、close 工具保持为薄 Adapter；`NativeAgentRuntimeFactory` 为每个 child 创建独立 `ContextEngine`、`PydanticAIModelDriver` 与唯一的 `LumenAgentLoop` Runtime。child Gateway 严格收窄父能力，writable child 的本地工具重绑定到独立 worktree，MCP 只复用父级连接 Adapter，不共享执行结果或幂等状态。v10 增加 Session 推理选择与 child 推理快照；v9 由 Realtime `live_session` record 引入，不改变 v8 Agent record 的语义。

V1 不引入 LangGraph。Lumen 已有 Session/EventJournal、RunCoordinator、TaskWorkspace、审批、ArtifactStore 和恢复协议；引入第二套 checkpoint/graph persistence 会产生双重状态权威。未来只有在单一 Agent 内确实需要可复用的确定性图执行、且能由 Session journal 统一提交时，才重新评估 LangGraph。

## 模块边界

- `AgentOrchestrator`：线程、代次、调度、消息、恢复、证据和完成门禁。
- `AgentRuntimeFactory`：捕获父轮次有效配置，创建受限 runtime，并实现 worktree 导入/拒绝。
- `AgentProfileLoader`：加载 builtin、user、trusted-project 角色并验证其只能收窄能力。
- `SessionRepository`：保存 `agent_thread`、`agent_event`、`agent_message`、`agent_result`，不执行调度。
- `WorkspaceHost`：绑定根 Run、上浮事件与审批，向 TUI/Web/headless 暴露同一命令契约。
- `TaskWorkspace`：只追踪导入主工作区后发生的 mutation 与验证；不拥有 Agent 生命周期。

## 安全与恢复不变量

1. Agent ID 只能由所属父 Session 操作；绑定根 Run 时禁止跨 Session 查找。
2. child 工具集等于父级实际有效工具、角色 allow-list 与 workspace mode 三者交集。
3. child 不获得 Agent 控制工具，因此 V1 最大深度由能力层强制为一。
4. read-only Agent 可从已持久化 transcript 边界恢复；未知外部副作用不得自动重放。
5. writable Agent 的改动只存在于独立 worktree，直到显式导入。
6. 导入不覆盖父工作区已有未提交改动；路径重叠或三方冲突进入 `reconciliation_required`。
7. terminal 状态不等于可完成：结果送达、失败 resolution、import/reject、Plan evidence 和 TaskWorkspace verification 都必须满足。

## 兼容性

2026-09-08 并发与执行更新：默认 `max_concurrency=3`、每 Run 上限 8；spawn 按序登记、
立即返回后后台执行，调度仍由同一个 Orchestrator 和每 Session Semaphore 管理。模型指令
明确要求先派发已知独立任务，再做父任务工作，依赖结果时等待剩余 Agent；不会自动分析依赖
或为了并发拆分简单任务。`explicit` 增加对应模型指令，不是新的许可或任务拆分算法。
重放相同 spawn 即使数量已达上限仍返回原 Thread，登记锁防止异步检查期间重复创建。

worktree Git 复用工具命令的异步子进程执行 Seam，替代 async 路径中的 `subprocess.run`；
取消和超时会终止进程组并回收输出，父进程先退出也会清理持有管道的后代。Git 命令串行锁
保护共享元数据，导入另有事务锁；不会阻塞其他 Agent 的模型流或事件循环。Git 输出截断时
拒绝作为完整 evidence。writable 恢复先记录 reconciliation 阻止完成，再异步检查，不能重放
未知动作；仍保留只读恢复、dirty path 和三方冲突检查、导入验收与父权限收窄。

child usage 增加 `agent_queue_seconds`、`worktree_prepare_seconds`、
`worktree_finalize_seconds` 和 `worktree_import_seconds`（适用时记录）。队列指标从调度
task 开始计时；worktree 准备包含等待 Git 锁，导入包含等待导入锁。它们不是 Provider usage。
推理快照优先级、原始配置兼容及 Session v10 见[第 7 章](07-configuration-and-data.md)。

2026-09-04 实施对齐：原生 Agent 的 `request_count`、`tool_calls`、`timeout_seconds` 默认 `null`，
长任务不再被默认 10/20 次或 180 秒截断。Factory 捕获配置时，对父/child 显式请求和工具预算取
更严格值；并发、最大深度、每 run Agent 数量、权限收窄和完成门禁不变。child 使用同一 Loop
滑动空闲、重试和 ContextEngine 同轮压缩逻辑。旧 delegation 的有限默认值仍属于兼容输入。

旧 `delegation` 配置映射到 `agents` 并产生弃用警告；同时出现时由 `agents` 决定。旧 child 工具与 Host API 通过 Adapter 操作新线程或只读展示历史 Child Run，不成为新的状态权威。v1-v7 Session 以空 Agent 状态加载；v8 直接读取 Agent records；任何历史文件都不改写 header，新增高版本事实时只追加 upgrade marker。
