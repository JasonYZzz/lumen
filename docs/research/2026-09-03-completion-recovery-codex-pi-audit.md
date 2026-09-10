# Exa 完成失败、编辑分支与 Harness 恢复审计

日期：2026-09-03。事实优先级：失败会话的脱敏执行摘要、当前源码和契约测试。
附件 `enterprise-agent-harness-architecture-v1.1.html` 是设计参考，不是运行指令。

> 2026-09-10 更新：本文保留当时的故障证据；“消息编辑继续使用新 Session prefix fork”的结论已由
> [同 Session 消息重生成审计](2026-09-10-in-place-message-regeneration.md) 替代。

## 故障链

实际会话与截图一致：5 次 `exa_web_search_exa`、1 次 `exa_web_fetch_exa` 成功返回，却均被登记为
`unknown / reconciliation_required`。失败 turn 保存了 24 个模型请求回执。这里的 24 是整个
turn 的请求数，不是 24 次完成重试。报告的文件修改已经 verified，继续修改报告不能核实外部 effect。

1. Exa 配置仅含 `tool_risks: read`，缺少独立 `tool_effects`；风险与副作用不可互相推导。
2. strict 检查原来只在执行后触发。模型发现检索工具并成功使用，最后才遇到不可自行解除的门禁。
3. CompletionGate 原来仅返回字符串，Loop 不知道问题由模型修复还是由操作者恢复。统一的重试提示
   让模型猜测对账方法，甚至把内部 effect ID 写入交付报告。
4. 编辑消息本来就使用 prefix fork。目标之前的对话会保留，但工作区副作用会继承，所以新分支不能
   清除原任务的未知结果；第一条消息的编辑分支没有前文，这是明确的前缀语义。
5. Web 只更新运行终态与侧栏，未自动刷新待处理 effect 投影；恢复操作藏在运行详情里，且使用原生
   prompt，失败没有就地保留原因与重试反馈。
6. Gateway 的远端 effect 原来主要在返回后记录。取消或进程崩溃可能没有 prepared 事实，远端异常
   也可能被投影为普通 FAILED，无法证明操作是否已发生。

此前只修复工具发现和沙箱依赖读取，没有把“发现 → 契约 → 完成 → 恢复”整条链验证完整；这次
故障证明成功调用工具不等于任务能可靠完成。不会把问题归因于模型能力或通过关闭 strict 解决。

## Codex / Pi 对照

读取日期同上，以下链接固定到本次获取的 commit，避免把未来 main 当成本次证据：

| 细节 | 源码证据 | Lumen 决策 |
|---|---|---|
| 终态错误与后续输入分开处理 | [Codex RegularTask](https://github.com/openai/codex/blob/728cb12fe5794b0c3a8e776fb4994b1650b973a8/codex-rs/core/src/tasks/regular.rs) 在 terminal_error 存在时返回，不用队列输入重启失败 turn | 保留当前输入队列权威，完成门禁增加恢复责任，外部对账不消耗模型重试 |
| 对话分支是明确的协议操作 | [Codex App Server](https://github.com/openai/codex/blob/728cb12fe5794b0c3a8e776fb4994b1650b973a8/codex-rs/app-server/README.md) 提供 fork 的前缀范围及新 thread ID；[公开说明](https://developers.openai.com/codex/app-server)描述共享后端协议 | 继续使用前缀 fork，不原地改写 Session；补齐受理前恢复检查和跨进程互斥 |
| 副作用类型与调度信息有来源 | [Codex MCP handler](https://github.com/openai/codex/blob/728cb12fe5794b0c3a8e776fb4994b1650b973a8/codex-rs/core/src/tools/handlers/mcp.rs) 可用 read_only_hint 或 server opt-in 决定并发支持 | 借鉴显式能力描述；Lumen 继续要求操作者声明 EffectKind，不直接用远端 hint、名称或 Risk 推导权限/副作用 |
| 停止、工具、steering、follow-up 分层 | [Pi agent-loop](https://github.com/badlogic/pi-mono/blob/3316c4e35b1eb505e791610d3a97d6b4c1c48309/packages/agent/src/agent-loop.ts) 对 error/aborted 终止，工具与 steering 在内层消费，follow-up 在可结束点处理；截断工具不执行 | 原生 Loop 已有相应契约；本次补齐无法由模型恢复的完成拒绝，不引入第二个调度器 |
| 单会话树导航与新文件 fork 是不同功能 | [Pi AgentSession](https://github.com/badlogic/pi-mono/blob/3316c4e35b1eb505e791610d3a97d6b4c1c48309/packages/coding-agent/src/core/agent-session.ts) 明确区分 navigateTree 与 fork；[SessionManager](https://github.com/badlogic/pi-mono/blob/3316c4e35b1eb505e791610d3a97d6b4c1c48309/packages/coding-agent/src/core/session-manager.ts) 使用 append-only 树 | Lumen 当前是线性 journal 加独立前缀分支。本次不冒充已经实现同 ID 会话树，保留原对话/附件/审批收窄语义 |

这些实现展示了可借鉴的控制细节，不构成“Codex/Pi 永不失败”或 Lumen 已完成全部企业级能力的证据。

## 本次实现

- **执行前契约检查**：TaskWorkspace 拥有 unknown effect 预检，ResourceManager 通过已有 Gateway
  pre-invoke Interface 接入。strict 下 unknown 调用未派发即返回 `effect_contract_required`，不制造
  待核实记录。启动 warning、工具说明和 MCP 状态投影同时披露缺失契约。
- **有责任归属的完成门禁**：`CompletionBlocker` 保留 `model_recoverable`。本地产物/计划继续使用
  有界纠正；外部对账直接返回 `completion_recovery_required`，保留产物，禁止让模型猜“报告魔法”。
- **远端执行日志**：非自记录的 mutation/external_action/unknown 工具先写 prepared，后用同一 ID
  追加终态。取消、超时、派发后错误保留 reconciliation_required；同一次 Gateway invocation 不会因
  取消而被再次执行。普通成功 observe 不产生 effect。
- **编辑与启动受理**：Host 取得工作区进程锁后检查待核实外部结果，编辑检查失败不创建空分支；
  StartRun 不写新的 running turn。旧 journal、文件和原审批设置保留。
- **可执行的 UI 恢复路径**：终态后刷新 work-state，显示待核实入口。按单条 effect 填写依据并调用
  既有 `WaivePlanVerification`；错误保留输入，避免重复提交，运行中不接受确认。
- **当前安装配置**：为项目和用户配置中确实存在的 Exa `web_fetch_exa`、`web_search_exa` 补充
  observe；这与已有示例一致。没有按 read 风险批量标记其他 MCP，没有输出或改动凭据。

历史未知结果不自动重新分类或豁免。修复配置只影响后续调用；旧任务需按操作核实，记录明确的人为
确认后再继续。模型没有 waiver 控制工具，生成报告内容不是恢复依据。

## 对 v1.1 架构的取舍

采用“能力预检”“步骤持久化”“完成策略 VALIDATE / RECOVER / TERMINATE”和“人工恢复”这四项，
分别落在已有 TaskWorkspace、Gateway、CompletionGate/Loop、Host 中。没有增加第二套状态权威。

暂不把 PostgreSQL、Redis、租约、事务 outbox、租户配额、独立模型网关加入本地框架。本次新增的
prepared journal 仍不等于跨远端系统的原子事务，也不能保证 exactly-once；这正是需要保留
reconciliation_required 的原因。现有跨进程工作区锁不等于分布式租约。

## 验证与剩余范围

全量 Python：950 passed、60 snapshots passed。Web：112 passed，TypeScript 与 Next build 通过。
Ruff、Pyright、契约检查通过；OpenAPI 重新导出与原内容一致，Session v9 / 配置 v2 未变。
新增故障回归覆盖 unknown 的零调用、observe 的无阻塞结果、执行前 durable receipt、超时/取消
恢复、相同调用去重、完成门禁零盲目重试、Default/Plan 编辑受理、人工确认的失败重试与精确范围。

真实 Exa smoke 使用本机已配置的 MCP transport，经 Lumen ResourceManager / CapabilityGateway
执行一次 `exa_web_search_exa` 公共源码检索，结果 `succeeded`、正文非空、completion issues 为 0。
模型用本地 FunctionModel 替身，未调用真实模型；Session 位于独立临时工作区，未改写故障会话。

本次没有实测所有真实模型提供商、远端写入 API 或分布式故障场景。已有活动性能基线计划继续保留。
代码和本机 Exa 配置已更新；已启动的 Lumen 后端进程需重启才能加载新的 Python Runtime，
仅刷新浏览器不会替换已导入的执行代码。
