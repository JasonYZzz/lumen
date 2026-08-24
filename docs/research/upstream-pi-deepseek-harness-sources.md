# Pi 与 DeepSeek Harness 上游源码核查账本

> 核查日期：2026-08-24  
> 对照文档：[`lumen-pi-deepseek-harness-architecture-comparison.md`](./lumen-pi-deepseek-harness-architecture-comparison.md)  
> 范围：只核查该文档涉及的 Pi 与 DeepSeek Harness 架构主张；未扩展到其他仓库、版本或产品能力。

## 1. 固定基线与方法

| 项目 | 固定对象 | 本地 checkout | 核查结果 |
| --- | --- | --- | --- |
| Pi | `a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c` | `/private/tmp/lumen-arch-research.2wTsA8/pi-mono` | `HEAD` 与对象一致；`@earendil-works/pi-agent-core` 为 `0.84.2` |
| DeepSeek Harness | `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` | `/private/tmp/lumen-arch-research.2wTsA8/deepseek-harness` | `HEAD` 与对象一致；root 为 `0.1.1-rc.2`，README 标记 developer preview |

核查以固定 checkout 中的源码、测试、package metadata 和仓库内一手 subsystem 文档为证据。
下列链接均固定到对应 commit。DeepSeek checkout 是 promisor/部分物化 checkout：core 的实现 blob 可直接读取；
subagent 与 experimental Agent Teams 的实现目录未物化，因此相关结论只采用同一固定对象中已检出的官方
subsystem 文档、其中生成的 `type-equiv`/API 段，以及文档明确标注的源码路径，不据此外推未记录行为。

## 2. Pi：经典路径与 Harness v2 的边界

### 2.1 经典路径是当前可运行主路径

固定对象上的真实装配链比主文档中的一句简写更完整，但最终仍进入经典实现：

```text
CLI main
  → createAgentSessionRuntime(factory)
  → createAgentSessionFromServices()
  → createAgentSession()
  → new Agent(...)
  → new AgentSession({ agent, sessionManager, ... })
```

- CLI 的 factory 在 [`packages/coding-agent/src/main.ts`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/main.ts)
  调用 `createAgentSessionFromServices()`；该函数在
  [`agent-session-services.ts`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/agent-session-services.ts)
  转入 `createAgentSession()`。
- [`createAgentSession()`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/sdk.ts)
  构造经典 `Agent`，从 `SessionManager.buildSessionContext()` 恢复其消息，再构造 `AgentSession`。
- [`Agent`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/agent.ts)
  直接拥有 mutable `state.messages`、tool/model/system prompt、streaming 状态、abort controller，以及
  `PendingMessageQueue` 实现的 steering/follow-up 队列；`streamFn` 是模型调用 seam。
- [`runLoop()`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/agent-loop.ts)
  的内层循环处理 tool continuation 与 steering，外层在本应结束时领取 follow-up；每次请求前依次调用
  `transformContext()`、`convertToLlm()` 和 `StreamFn`。这与主文档描述一致。

经典路径的运行中 transcript 与持久 tree 确实是两种由胶合层同步的表示：

- [`AgentSession._handleAgentEvent()`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/agent-session.ts)
  订阅 `Agent` 事件，在 `message_end` 后调用 `SessionManager.appendMessage()`；源码注释明确说明
  `Agent` state 先持有 finalized message，持久化随后发生。
- [`SessionManager`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/session-manager.ts)
  的 entry 以 `id`/`parentId` 形成 tree，`leafId` 选择活动分支；`migrateToCurrentVersion()` 会原地迁移内存
  entries，而 `_setSessionFile()` 在迁移发生时调用 `_rewriteFile()`。因此主文档关于旧 session 可能整体重写的
  判断成立。

关键符号：`StreamFn`、`Agent`、`PendingMessageQueue`、`runAgentLoop()`、`runLoop()`、
`createAgentSession()`、`AgentSession._handleAgentEvent()`、`SessionManager.appendMessage()`、
`SessionManager.buildSessionContext()`、`migrateToCurrentVersion()`。

### 2.2 Harness v2 有真实数据层，但执行面仍是 scaffold

固定对象的 [`packages/agent/src/harness/`](https://github.com/earendil-works/pi-mono/tree/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/harness)
不是空目录。它已有 Session/record/reducer、memory 与 JSONL storage、lane state、compaction、skills、tools、
events/watch primitives 和 telemetry 类型；
[`createCodingAgentHarness()`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/server/create-harness.ts)
也已能装配 model、system prompt、资源与 coding tools。不过全仓生产源码中没有该 factory 的调用点；除定义外
只有 `packages/coding-agent/test/server/create-harness.test.ts` 使用它，所以它尚未接入 CLI/server 产品入口。

但这些不能被表述成已完成的替代 runtime：

- [`AgentHarness.create()`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/harness/agent-harness.ts)
  只接受无 record 的 Session；存在 record 时抛 `HarnessNotImplemented("create.restore")`。
- `prompt()`、`skill()`、`promptFromTemplate()`、`compact()`、`navigateTree()`、`resume()`、`abort()`、
  `steer()`、`followUp()`、`nextRun()`、queue control、idle/action APIs、lane APIs、`watch()` 与
  `watchSession()` 都进入 `unavailable()`，抛 `HarnessNotImplemented`；hooks/events registry 同样不可用。
- [`agent-harness-scaffold.test.ts`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/test/harness/agent-harness-scaffold.test.ts)
  的 suite 名就是 `AgentHarness v2 scaffold`，并逐项固定上述拒绝行为。现阶段可用的是无历史创建、部分
  defensive-copy configuration getter/setter 与 `close()`，不是模型/工具执行 loop。
- `packages/agent/src/harness/reducer.ts` 中的实际符号是 `reduceLaneState()`，并有测试；它没有从
  `packages/agent/src/index.ts` 或 package subpath export 暴露，属于仓库内 Implementation scaffold，不是 npm
  公共 Interface。

所以主文档用“经典路径评价现役产品能力、Harness v2 只作演进信号”的边界是准确的。

关键符号：`AgentHarness`、`HarnessNotImplemented`、`UnavailableRegistry`、`Session`、
`InMemorySessionStorage`、`JsonlSessionStorage`、`createCodingAgentHarness()`。

## 3. DeepSeek Harness：spine、scope、session、tool 与 subagent

### 3.1 Core spine 是 Interface/Implementation 分离的 package graph

[`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/core.md)
把 core spine 明确列为 `session`、`system-prompt`、`tools`、`agent`、`agent-loop`、`scope` 六个 package：

- `agent` 声明公共 `Agent` handle、live registry、initiator scope 与 `agent/*` 事件；
- `agent-loop` 提供默认具体 driver，并通过 `AgentRegistry.setFactory()` 注册 factory；
- extension 依赖 `agent`，不直接依赖 `agent-loop`，所以 loop 可替换；
- compaction、approval、sandbox、subagent 等位于该最小 spine 之外，以可选 capability/plugin 组合。

源码互证：

- [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent/src/index.ts)
  定义 `AgentRegistry`、`AgentFactory`、`AgentHandle`、`CreateAgentOptions` 与 `ResumeAgentOptions`。
- [`ReactLoopAgent`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/agent.ts)
  是公共 `Agent` Interface 的具体 Implementation；其 phase 是 `idle | maintenance | running`，公共 status
  只投影为 `idle | running`。

这支持主文档“Cordis 组合根 + 可替换 loop Implementation”的判断。

### 3.2 Scope 同时决定可见性与 effect ownership

- [`createScope()`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/scope/src/index.ts)
  以不透明对象 identity 作为 `ScopeKey`，创建带 scope tag 的 Cordis context；`Scope.dispose()` 会等待
  backing fiber quiescence。`ReactLoopAgent` 以 live `this` 创建 scope，并暴露 `agent.ctx`。
- [`ScopedLayers`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/scope/src/store.ts)
  拥有 eager global layer 与 lazy exact-scope layers；读取不创建 layer，`merge()` 先放 global 再应用
  ancestor-to-nearest scope shadow。`effect(ctx, action, ...)` 从同一 context 同时取得 visibility scope 和
  Cordis effect owner，并在撤销后回收空 layer。
- `scopeTarget()` 的事件 admission 沿 scope parent chain 向上，让 enclosing scope listener 观察 descendant，
  不反向泄漏到 child。

因此主文档把 scope 概括为“visibility + lifetime ownership”有直接源码依据。

关键符号：`ScopeKey`、`createScope()`、`scopeOf()`、`scopeTarget()`、`bindScopeParent()`、
`ScopedLayers.merge()`、`ScopedLayers.effect()`。

### 3.3 Session log 是 history 事实源；durability 是独立 seam

[`Session`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/session/src/index.ts)
维护 append-only typed `SessionEvent[]`；
[`SessionEventMap`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/session/src/types.ts)
包括 `turn/start|end`、`step/start|end`、user/assistant messages、raw `assistant/chunk`、tool call/result、
request header/context 等事件。`ReactLoopAgent` 的每个 step：

1. 先 append turn/step 与 entered user messages；
2. 用 `session.deriveMessages()` 生成 request history；
3. 每个 stream chunk append `assistant/chunk`，组装后 append `assistant/message`；
4. 工具调用 append `tool/call` 与 `tool/result`；
5. 最后 append `step/end`、`turn/end`。

[`surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/session/src/surface.ts)
把 `user/message`、`assistant/message`、`tool/result` 定义为 model-visible surface；append-only 原 log 保留，
replacement 只改变下次 `deriveMessages()` 看到的 ordered surface。不存在第二份独立 mutable message history。

需要保留一个精度边界：`Session` 是内存事实源，不等于已经 durable。
[`persistence.md`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/persistence.md)
明确把 durability 交给独立 `SessionPersistence`/backend controller；`session/event` 同步进入 write-behind
controller，`session/flush` 取消 batching wait 并 drain 至 quiescence。下一 ordinary turn 前的 barrier 由
`dsh-session-checkpoint-policy` 拥有，不是 `ReactLoopAgent` 在 turn boundary 内硬编码的 flush。

[`interruptedTurnClosers()`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/session/src/repair.ts)
不截断 crash tail：它为未配对 tool call 追加 `TOOL_NOT_STARTED` 或 `TOOL_OUTCOME_UNKNOWN` result，再补
`step/end` 与 `turn/end { interrupted }`，保持 provider transcript 平衡。该行为支持主文档的恢复判断。

关键符号：`Session.append()`、`Session.deriveMessages()`、`SessionStore.flush()`、`SessionEventMap`、
`SessionSurface`、`deriveEventMessage()`、`interruptedTurnClosers()`。

### 3.4 ToolRuntime 收口 policy，scheduler 保持模型顺序

[`ToolRuntime`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/tools/src/index.ts)
统一处理 scoped definition resolution、restriction、execution mode、`tools/pre-execute` waterfall、monotonic
guards、`tools/execute` around waterfall、body、`tools/post-execute`、内容 finalization/materialization 与
`tools/result` notification。`guard()` 只能返回 denial reason 或 abstain，不能 force-allow；approval seam 缺失或
返回非 `allowed-once` 时会 fail closed。

[`executeToolCalls()`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/tool-calls.ts)
的调度证据是：

- exclusive call 形成 barrier；parallel call 使用有界 rolling pool；
- 未启动 call 在启动前重新读取 `executionMode()`，所以 live registry 变化可建立新 barrier；
- pre-execute 与 result finalization 按模型源顺序进行，只有 around-dispatch/body 可以重叠；
- abort 停止补充新 call、等待已启动 call settle，并为未启动 call 写成对的 synthetic aborted result；
- `tool/result` 与 additional context 仍按模型 call 顺序提交。

这与主文档的 pipeline、deterministic concurrency、abort/drain 描述一致。

关键符号：`ToolRuntime.executionMode()`、`ToolRuntime.guard()`、`prepareScheduledExecution()`、
`dispatchScheduledExecution()`、`finalizeScheduledExecution()`、`executeToolCalls()`、`runGroup()`。

### 3.5 Subagent 是可选 named-provider seam，continuation 以 child Session 为 identity

同一固定对象的
[`docs/subsystems/subagent.md`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/subagent.md)
记录并生成了下列源码契约：

- `ctx.subagents` 是 named Provider registry；in-process spawn/fork、ACP、Codex、Claude Code、DSH SDK provider
  可以并存。`SubagentCapabilities` 对 `outputSchema`、depth limit、tool filter、persona 做 start 前检查，缺能力
  抛 `UNSUPPORTED_CAPABILITY`，不接受后静默忽略。
- one-shot 路径返回 `SubagentRun`；continuable 路径由 continuation manager 管理，一个 durable child Session
  同时最多一个 process-local `Activation`，持有一个 `AgentHandle`。Agent inbox 是唯一 FIFO，不另建 Task 或
  第二执行队列。
- `startContinuable()` 建立稳定 child id；`followup()` 根据 resident/waiting/cold 状态复用或重建 Activation；
  冷恢复调用 `ctx.agents.resume()`。direct-parent authority 来自持久 `SessionHeader.parentSession`，
  `MessageSource` 只作 attribution。
- delegation depth 同时存在于 durable Session metadata 与 runtime checks。fork provider 的 seed 是从 seq 0
  到父级最后一个 `turn/end`（含）的 balanced completed-turn prefix，排除 in-flight 不平衡 turn。

文档标注的 Implementation 路径是 `packages/subagent/subagent/src/types.ts`、`src/index.ts`、
`src/continuation.ts`；关键符号是 `SubagentRuntime`、`SubagentProvider`、`SubagentCapabilities`、
`SubagentRun`、`Activation`、`ContinuableCreateSpec`。

实验性 Agent Teams 的
[`agent-team.md`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/agent-team.md)
还固定了 implicit-root Team identity、durable roster snapshots、queued-minus-delivered mailbox 与带 revision/CAS 的
shared task DAG；`foldTeam()` 从 root Session 重放这些投影。它确实比普通 subagent seam 更宽，但文档明确称为
experimental。因此主文档把它作为实验能力、而非 core loop 默认保证，是准确的。

## 4. 与主文档的冲突检查

**总体架构判断成立，但发现两处明确的 Pi 符号/API 表述冲突，以及一处 durability 措辞风险：**

1. 主文档 §4.7 写成 `reduceLane()`；固定对象的实际符号是
   `packages/agent/src/harness/reducer.ts::reduceLaneState()`，全仓没有 `reduceLane()`。这是明确错名。
2. §4.7 把该方向总括为“已导出的类型与已测试的 scaffold”过强：`reduceLaneState()` 虽有测试，但未从
   `packages/agent/src/index.ts` 或 package subpath export 暴露。准确说法应是“仓库内已有并测试的 reducer
   scaffold”；不能把 reducer Implementation 算作已导出的公共 API。
3. §3.2 在 `durability-before-publication` 后说“Pi 经典路径更直接”，若被读成 Pi 也提供同类 durable guarantee，
   会造成错误推断。经典 `AgentSession._handleAgentEvent()` 先向 extension 和 UI listeners 发布事件，随后才在
   `message_end` 调 `SessionManager.appendMessage()`；持久实现使用 `appendFileSync`/`writeFileSync`，未见 `fsync`，
   且首批 user entries 可等 assistant 出现后才整体写入。Pi classic 不具备 Lumen 式
   durability-before-publication。原句只有在“流程较少”这一窄义下成立。

另外三处是精度补充，不反转主结论：

1. Pi CLI 并非从 `main.ts` 直接一跳调用 `createAgentSession()`；当前链路先经过
   `createAgentSessionRuntime()` 和 `createAgentSessionFromServices()`，最终仍由 `createAgentSession()` 构造经典
   `Agent`/`AgentSession`。主文档的链路是架构简写，不是另一套 runtime。
2. DeepSeek `Session` log 是内存中的 canonical interaction/history source；durability、batching、flush barrier 与
   crash-load repair 由独立 persistence/checkpoint policy seam 完成。主文档已把 persistence 视为独立 Interface，
   但阅读“Session event log 是事实源”时不应误解为每次同步 `append()` 已经落盘。
3. `createCodingAgentHarness()` 确实存在并有测试，但没有 production reference；“coding-agent 有装配器”不能被
   外推成 Harness v2 已接入当前 CLI/server。

除上述具体问题外，Pi 经典路径/Harness v2 scaffold 分界，以及 DeepSeek spine、scope、session surface、tool
pipeline、deterministic scheduler 和 subagent continuation 的表述，都能在固定对象的一手证据中得到支持。
