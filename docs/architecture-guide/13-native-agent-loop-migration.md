# 13. LumenAgentLoop 单一权威决策记录

> **状态：Accepted（单轨已实施）**<br>
> **接受日期：2026-08-26**<br>
> **实施更新：2026-09-04（长任务恢复契约对齐）**

## 13.1 决策

Lumen 的单 Agent 主模型—工具循环由 `LumenAgentLoop` 唯一拥有。生产 turn 路径不再构造或调用
PydanticAI `Agent` graph，也不存在 engine selector 或隐藏回退分支。无工具的 Context 摘要和 Memory
结构化提取仍可使用 PydanticAI `Agent` 作为辅助 schema Adapter；它们不参与 turn、工具、恢复或完成
调度，因此不构成第二套 Agent Loop 权威。

PydanticAI 继续提供有价值的低层 Provider Adapter。当前调用链是：

```text
WorkspaceHost
  → RunCoordinator
  → AgentRuntime
      → ContextEngine
      → LumenAgentLoop
          ├─ PydanticAIModelDriver
          │    └─ PydanticAI Model / Provider Adapter
          ├─ CapabilityGateway
          └─ CompletionGate
      → SessionRepository / TaskWorkspace / AgentOrchestrator
```

本决策不是“Lumen 全面超越 PydanticAI”，而是重新分配最适合双方拥有的权威：

- Lumen 拥有 Session、Context、请求证据、工具、审批、Hook、Effect、恢复、完成门禁和多 Agent 生命周期；
- PydanticAI 保留 Provider wire translation、公开 `Model` API、`ModelMessage` 兼容和必要的 schema validation；
- 不把移除 `pydantic-ai` 依赖、原生 Provider Driver 或 Session v10 作为本次完成条件。

## 13.2 当前唯一权威

| 语义 | 唯一权威 |
|---|---|
| turn 外壳、Context prepare/commit、公开事件、partial outcome | `AgentRuntime` |
| 模型请求、工具批次、继续、重试、取消、terminal candidate | `LumenAgentLoop` |
| Provider wire translation 与模型资源生命周期 | `ModelDriver` |
| 能力目录、参数验证、审批、Hook、guard、timeout、幂等、Effect | `CapabilityGateway` |
| 模型可见输入与预算 | `ContextEngine` |
| append-only canonical history | `SessionRepository` |
| 多 Agent 生命周期、证据和完成门禁 | `AgentOrchestrator` |

## 13.3 ModelDriver 与 Provider 保真

`ModelDriver.open_stream()` 返回一次流式会话。调用者消费有序 `ModelStreamEvent` 后，从
`stream.response` 取得 Provider Adapter 产生的完整或部分响应；Runtime 不再从文本和工具事件
反向重建 `ModelResponse`。

`PydanticAIModelDriver`：

- 调用公开 `Model.prepare_messages()` 和 `Model.request_stream()`；
- 翻译 text、thinking、tool call/arguments、usage/cache、finish reason、response state 和 response id；
- 把 provider-private/native part 保留在现有 `ModelMessage` 中，不扩散到公开 `RunEvent`；
- 拥有底层 `Model` 的异步生命周期，并由 `AgentRuntime` 以 lease 管理；
- 使用公开 `continuation_delay()` 与 `cancel_suspended_response()`；
- 不执行工具、审批、Session 写入或完成判定。

`ReplayRecording` 同时保存事件与 exact/partial response，并只按
`ModelInputManifest.request_fingerprint` 精确匹配。

## 13.4 请求证据、继续与完成

每个新的逻辑模型请求都在 `AgentRuntime._freeze_lumen_request()` 中生成有界
`ModelInputManifest` 和 `ProviderRequestReceipt`。Manifest 保存 route、step、Context
fingerprint，以及 instructions、消息、工具 schema、Context source、stable prefix 和 dynamic tail
的 digest；它不是第二套历史。同一冻结请求的 transport 重试复用 manifest；
`model_attempts` 与 `model_attempt` / `model_retry` 诊断记录实际尝试，不能把 receipt 数量当成尝试数。

Loop 统一管理默认 300 秒流空闲计时、有界指数退避和重试取消。可见候选文字可撤回再生成，
不再作为禁止重试的条件；已完成工具结果保留，Provider 内置工具活动禁止透明重放。
OpenAI/Anthropic 主模型 SDK retries 为零。请求/工具次数与总时限默认不设硬截止，显式预算仍有效。
ContextEngine.prepare_step 在同轮请求间压缩，context overflow 最多触发一次有效压缩后的恢复。
这些机制的完整条件与故障分类见[第 14 章](14-long-running-recovery.md)。

suspended response 不参与完成判定，也不执行未闭合工具调用。同一 response id 最多后台轮询
1000 次，新 generation continuation 最多 10 次；取消、超限或异常会请求 Provider 取消。
进程硬中断后不会猜测恢复未持久化的 Provider job。

`CompletionGate` 直接返回结构化 issue。Loop 把 issue 作为 typed retry request 进入下一步；
达到上限后以 `LoopCompletionRejected` 和 partial outcome 失败，不再通过 PydanticAI
`ModelRetry` 或 output validator 间接控制循环。

终止原因按穷尽矩阵 fail closed：只有 `END_TURN` 可以进入完成门禁，只有 `TOOL_CALL` 可以执行工具；
`LENGTH` 不会执行未闭合工具调用。若 `max_tokens` 来自隐式/profile 默认值，Runtime 可在请求边界有界
扩大输出预算并重新 preflight；文本截断只在存在 exact partial response 时续写，工具参数截断则从原请求
重新生成。显式用户上限、架构上限或重试次数耗尽后形成可行动的截断失败。`CONTENT_FILTER`、`REFUSAL`、
`ERROR` 与 `UNKNOWN` 都不能执行工具或伪装完成。usage-only provider activity 会累计到 usage。

Runtime 在产生 terminal candidate 前验证 exact response、生成 canonical messages 并提交 Context
candidate；Coordinator 将 terminal 与 turn 一起 append + `fsync` 后才向客户端发布，并保证每个 run
最多一个公开 terminal。

## 13.5 Capability 与 deferred MCP

所有本地和 MCP 工具只能经 `CapabilityGateway`。调用顺序固定为：

```text
prepare → pre Hook → approval → guard → invoke/replay → effect → post Hook → model result
```

post Hook 只能改变 model-visible 文本，不能改变 canonical output、Effect 或 presentation。
`ToolConcurrency` 在 invocation time 计算；`EXCLUSIVE` 始终形成 barrier，结果按 Provider
call 顺序回填。

deferred MCP 使用 run-local 可见集合，不创建第二个 Registry：

1. 初始仅暴露 always-loaded capability；
2. 存在未加载 capability 时暴露保留控制能力 `search_tools`；
3. 本地稳定 token overlap 搜索最多返回 10 项；
4. 搜索结果写入现有 `ToolSearchReturnPart`；
5. 被发现 capability 从下一次请求开始暴露完整 schema；
6. 历史中的搜索结果可恢复已发现集合；
7. 伪造的未加载调用返回 typed `tool_not_loaded`。

`search_tools` 与本地、插件或 MCP 名称冲突会在资源构建阶段失败。

## 13.6 Surface、child 与恢复

TUI、Web/SSE 和 headless 继续只消费稳定 `RunEvent`；OpenAPI 和客户端 DTO 没有 engine 字段。
Realtime 保持独立 transport Adapter，但共享 Gateway、Effect 和 CompletionGate。

`NativeAgentRuntimeFactory` 创建完整 child Lumen Runtime：

- 独立 `ContextEngine`、`PydanticAIModelDriver` 与异步生命周期；
- capability 严格等于父有效集合 ∩ role allow-list ∩ workspace mode；
- MCP Adapter 可继承父连接，但 Gateway result/idempotency 状态不共享；
- writable child 的本地 capability 重绑定到独立 Git worktree；
- child 不获得多 Agent 控制能力，最大深度仍为一。

恢复只按精确参数签名回放成功副作用；read/control 不伪造 Effect，unknown external effect 不自动
重放。pending approval、未验证 mutation、未送达 child result、冲突或 reconciliation issue 都会
阻止根 Agent 完成。

## 13.7 已删除内容与证据

以下对象已从主模型—工具生产路径删除：

- `AgentLoopEngine` 与 `loop_engine` selector；
- `AgentRuntime.agent` 与 `Agent.run_stream_events()`；
- `ProviderRequestPreflight`；
- deferred approval 的 `HandleDeferredToolCalls` Adapter；
- output validator / `ModelRetry` completion Adapter；
- `HookedFunctionToolset`、`HookedToolset` 与私有 `_function_toolset` 修改；
- `parallel_tool_call_execution_mode` 旧调度；
- 只验证上述旧 graph 的 fixture 和测试。

删除依据是同一模型—工具 Loop 权威已由 Lumen Implementation 完全替代且对应入口生产引用归零。保留
`PydanticAIModelDriver`、PydanticAI Provider Adapter、Session v9 `ModelMessage`、有实际价值的
ToolDefinition/schema Adapter、无工具的结构化摘要/提取 Adapter，以及 Provider/Session/公开 Interface
契约测试。

发布后不提供隐藏回退开关。若预发布 Provider smoke、性能或外部环境门禁失败，应回滚上一发布版本，
不能让已经产生 Effect 的活动 run 切换实现并重放。

## 13.8 非目标与未来 ADR

以下内容不属于本决策的关键路径：

- Session v10 或新的 canonical message schema；
- Lumen 原生 OpenAI、Anthropic、Google、Ollama Driver；
- 公开 YAML/CLI Loop selector；
- 线上有副作用的双 Loop shadow；
- 复制当前没有使用价值的 PydanticAI capability。

这些方向只有出现独立、可量化收益时才提出新 ADR。当前成功标准是“Lumen 运行时语义单一权威，
Provider 能力不发生不可接受退化”，而不是依赖数量或代码行数。

## 13.9 验证与发布门禁

旧实施计划已由本决策及当前源码替代，仍有效的门禁集中维护于此：

- Provider 保真：当前生产 Provider 的脱敏 conformance 与凭据 smoke，包括 text/thinking、
  tool arguments delta、usage/cache、private part 以及 stop/length/refusal/suspended/unknown；
- 执行与恢复：参数冻结、deferred 发现、审批、Hook、并发、timeout/cancel、unknown effect 对账、
  附件、澄清、steer/follow-up，以及 Agent 权限交集、worktree 导入与完成门禁；
- Surface 与持久化：Default、Plan、TUI、Web/SSE、headless 共用同一契约；Session v1–v9 可读，
  加载不改写，实际请求均生成 Manifest/receipt；
- 静态检查、测试、生成物与打包按仓库 `AGENTS.md` 的风险分层执行；从构建 wheel 执行
  `lumen --version` 与 `lumen --check-config`；
- 目标部署环境验证资源清理，取消/超时后不遗留 stream、task、client 或进程树；相对基线，
  排除 Provider/工具耗时后的 Loop p95 增幅不超过 5%，TTFT median 额外开销不超过 20ms。

本地 fixture 通过不代表真实 Provider smoke 或性能已验收。尚未完成的性能基线采样仍由
[活动精简计划](../plans/2026-09-02-pi-harness-v2-dongbi-v7-upgrade-plan.md) 跟踪。
门禁失败应停止发布；回滚单位是上一发布版本，不能让已产生 Effect 的活动 run 切换 Loop 并重放。
