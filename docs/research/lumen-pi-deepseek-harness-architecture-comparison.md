# Lumen、Pi Agent 与 DeepSeek Harness：源码级底层原理与架构对比

> 调研日期：2026-08-24  
> 证据范围：Lumen 当前本地 worktree；Pi 与 DeepSeek Harness 官方 GitHub 仓库的固定 commit。  
> 方法：以源码、契约测试、Accepted 架构记录和官方仓库文档为一手证据；不以社区文章推断内部实现。

> **实现跟踪（2026-08-27）：** 本文的对比数据与外部 commit 保持为 2026-08-24 研究快照，不据此
> 覆盖当前实现。Lumen 已完成 `LumenAgentLoop` 单轨切换，保留低层 `PydanticAIModelDriver` 和请求
> `ModelInputManifest`；PydanticAI `Agent` graph 与 selector 已删除。当前事实以
> [实现审计](../architecture-guide/12-current-implementation-audit.md) 与
> [迁移决策记录](../architecture-guide/13-native-agent-loop-migration.md) 为准。

## 0. 版本基线与结论边界

| 项目 | 本次基线 | 状态说明 |
| --- | --- | --- |
| Lumen | 本地 `9c924cc221d79a0f876804852f5881bf54af8e3c` + 2026-08-24 当前未提交修改 | 本地源码与契约是最高事实来源；未提交实现无法用 GitHub permalink 精确表示 |
| Pi | `a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c`，`@earendil-works/pi-agent-core` 0.84.2 | 官方仓库 `earendil-works/pi-mono`；经典运行路径已成熟，Harness v2 仍是 scaffold |
| DeepSeek Harness | `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`，root 0.1.1-rc.2 | 官方仓库 `deepseek-ai/deepseek-harness`；仍是 RC / developer-preview 语义 |

这里的 **Pi Agent** 指 Pi coding agent 及其底层 `pi-agent-core`，不是同名 Python SDK 或第三方 fork。
这里的 **DeepSeek Harness** 指 DeepSeek 官方开源仓库，不是 DeepSeek 模型 API 本身。

最重要的总体结论是：三者都运行“模型 → 工具 → 观察 → 再请求模型”的迭代 loop，但它们真正不同的
不是 ReAct 外形，而是 **谁拥有状态、何时把事实变成 durable、扩展如何获得能力、失败后如何判断可以继续，
以及一个 Agent 是否有资格宣称完成**。

## 1. 执行摘要

### 1.1 三种架构人格

1. **Pi：最小 loop + 应用层自由组合。** `Agent` 直接拥有消息数组、流式状态、steering/follow-up 队列和
   工具 loop；`AgentSession` 再把它接到 coding-agent 的 JSONL tree、compaction、extensions 与 TUI。
   它的优势是透明、轻、Provider 面广；代价是 sandbox、审批、Plan、多 Agent 和外部副作用恢复不属于默认
   core 的系统保证。
2. **Lumen：由深 Module 拥有不同状态概念，并通过 Host 汇合。** `WorkspaceHost` 是客户端 seam，
   `RunCoordinator` 拥有 turn 协调，`AgentRuntime` 拥有单 Agent loop，`ContextEngine` 拥有上下文两阶段
   prepare/commit，`TaskWorkspace` 拥有 mutation/effect 验证，`AgentOrchestrator` 拥有多 Agent 生命周期。
   其设计目标不是最小代码量，而是保持每个概念只有一个运行时权威，并让完成声明受到持久化与验证证据约束。
3. **DeepSeek Harness：事件溯源 spine + Cordis 插件微内核。** Session event log 是模型历史与恢复的事实源；
   `agent` 只定义公共 handle，`agent-loop` 是可替换 Implementation；Tool、Prompt、Approval、Sandbox、
   Compaction、Subagent 都通过 scoped registry / waterfall 组合。它的 Leverage 极高，但有效行为分散在大量
   package、scope 与插件监听器中，调试和认知成本也最高。

### 1.2 一句话判断

- 想要一个可嵌入、易改造、尽量少做平台判断的 Agent loop：Pi 最合适。
- 想要一个对工作区 mutation、审批、恢复、客户端一致性和完成证据负责的应用框架：Lumen 的方向更完整。
- 想要一个可热插拔、可多实现替换、事件与 scope 语义高度系统化的 Agent 产品微内核：DeepSeek Harness
  的抽象能力最强，但不能低估其 package graph 和插件组合复杂度。

### 1.3 对 Lumen 的核心建议

Lumen 不应整体模仿 Pi 的“把策略交给 extension”，也不应整体复制 DeepSeek Harness 的数百 package
微内核。建议保留现有深 Module 和唯一权威，同时选择性吸收四点：

- 从 Pi 吸收 **低层 loop 的可读性与可替换 stream seam**，并把经典 loop 做成更小的契约图，而不是继续
  扩大 `AgentRuntime.run()` 的认知面积。
- 从 DeepSeek Harness 吸收 **scope = visibility + lifetime ownership** 的统一思想，继续强化 Lumen 已有的
  `RegistrationScope`，但不把领域状态迁入通用 scope framework。
- 引入 **Sandbox enforcement fact**：除了配置 mode，还对外报告 backend、full/partial/unavailable、网络
  与进程可见性是否实际受控，避免 UI 把配置意图显示成已兑现保证。
- 将外部 Agent 接入建模成 `AgentRuntimeFactory` 的 backend，而不是第二个 Orchestrator；生命周期、worktree
  import、证据与 completion gate 继续由 `AgentOrchestrator` 独占。

## 2. 共同底层原理：Agent 不是一次模型调用

三个系统的最小闭环都可以抽象成：

```text
input
  → assemble(system / history / tools / dynamic context)
  → stream model response
  → if tool calls:
        validate / authorize / schedule / execute
        append tool observations
        repeat model request
    else:
        finish turn
  → persist / project / expose outcome
```

这个 loop 的技术难点不在 `while`，而在五个一致性问题：

1. **模型看到的 history 从哪里派生？** 是 mutable message array、Session tree，还是 append-only event log？
2. **工具已经产生副作用，但进程在记录结果前崩溃怎么办？**
3. **并行工具按完成顺序返回，但模型上下文按源顺序要求确定性，谁负责重排？**
4. **压缩后的 active context 与完整 canonical history 如何同时存在而不形成双权威？**
5. **模型输出了“完成”，但还有未验证文件、未交付 child result 或待审批动作，谁可以否决？**

Pi 优先优化 loop 的小与灵活；DeepSeek Harness 优先优化事件与插件组合；Lumen 优先优化应用级状态权威、
副作用审计和完成正确性。

## 3. Lumen：底层原理与技术架构

### 3.1 依赖方向与深 Module

```text
Textual TUI ─┐
Next/FastAPI ├─ Adapter → WorkspaceHost
Headless ────┘                 │
                               ├─ RunCoordinator → AgentRuntime → Provider / Tools
                               ├─ ContextEngine → ArtifactStore / Memory
                               ├─ TaskWorkspace → Work Product / Effect journal
                               ├─ AgentOrchestrator → AgentRuntimeFactory / worktree
                               └─ SessionRepository → append-only JSONL facts
```

`WorkspaceHost` 不是另一个模型 loop。它拥有 Session actor、active run、审批 future、幂等请求和 ordered
`EventJournal`，让 TUI、Web、headless 共享 command/event 契约。真正的单 Agent loop 在
[`AgentRuntime`](../../src/lumen/runtime.py)，turn 与持久化顺序在
[`RunCoordinator`](../../src/lumen/run_coordinator.py)，上下文在
[`ContextEngine`](../../src/lumen/context/engine.py)。

这种拆分通过 deletion test 才成立：删除任一深 Module，其隐藏的复杂度会散落到多个调用者，而不是只少一层
转发。当前 Accepted 设计与实现审计见
[`02-agent-runtime-flow.md`](../architecture-guide/02-agent-runtime-flow.md)、
[`10-native-multi-agent-runtime.md`](../architecture-guide/10-native-multi-agent-runtime.md) 和
[`12-current-implementation-audit.md`](../architecture-guide/12-current-implementation-audit.md)。

### 3.2 从输入到 durable outcome

Lumen 的关键顺序是：

1. Host 用 `(session_id, client_request_id)` 去重，拒绝同 workspace 第二个主 run。
2. Coordinator 先追加 `running` turn，证明输入已被接受，再进入 Provider 或工具执行。
3. Runtime 调 `ContextEngine.prepare()` 获得有 fingerprint 的 provider-ready envelope。
4. Pydantic AI 驱动模型/工具 loop；Lumen 翻译 streaming、thinking、tool、approval 与 work-product 事件。
5. Runtime 调 `ContextEngine.commit()` 生成候选 active history，但 checkpoint 还没有成为运行中事实。
6. Coordinator 追加 terminal turn 并 `fsync`；成功后才 `confirm_persisted()` 发布 checkpoint，再替换内存状态。
7. 如果 rich terminal payload 无法序列化，Coordinator/Host 还会追加最小 failed terminal record，避免 Web 已显示
   的文本在恢复后静默消失。

这里真正重要的是 **durability-before-publication**：内存 checkpoint 不得领先于 JSONL。Pi 经典路径的流程较少，
但不提供这一保证：事件会先发布给 extension/UI listener，随后才在 `message_end` 写入 Session，且持久实现未见
`fsync`。DeepSeek Harness 则把 Session 内存事实源与 persistence/checkpoint policy 分开，通过 write-behind、
`session/flush` 和 ordinary-turn barrier 建立耐久化 seam；这仍不同于 Lumen 在 terminal turn 上的显式两阶段发布。

### 3.3 Runtime：模型 loop 与推测式文本投影

`AgentRuntime` 以 Pydantic AI `Agent` 作为 Provider/tool loop Implementation，但 Lumen 自己拥有以下语义：

- request/tool call limits、transient retry 与 partial outcome；
- approval request/result 和 recovery receipt；
- Provider request receipt：route、token 分区、visible tool digest、context fingerprint；
- provider thinking 与普通文本分离；
- completion validator 联合 Plan、Work Product 与 Agent unresolved issues；
- AttachmentRef 在 Provider boundary 解引用，持久化前恢复为 artifact marker。

流式文本采用推测式渲染：响应中的文本先作为 `TextDelta` 显示；如果同一响应随后出现 tool call，就发
`TextRetracted` 并将该段重新投影为 commentary。这样 UI 不必等整个响应结束，同时不会把工具前说明误写成
最终答案。

### 3.4 ContextEngine：active context 不是 canonical history

Lumen 把上下文看成对完整 Session history 的预算化投影，而不是历史本身。`prepare()` 同时处理：

- Prompt、instructions、tool schema 和 safety reserve 的固定预算；
- History、memory、skills、retrieved context、task/work-product state 等 Zone；
- 大 tool output spill 到内容寻址 ArtifactStore，仅保留 receipt；
- rolling summary、V2 checkpoint parent/range/cursor/digest/provenance；
- anti-thrash、cooldown、后台候选与 stale candidate 拒绝；
- summary 失败后的 deterministic `degrade_to_window`，确保不向 Provider 发送超 hard limit 请求。

V2 checkpoint 以完整 source transcript 的范围与 digest 为权威，active prefix 只是 Provider 投影。
`prepare → commit → fsync → confirm_persisted` 是 Lumen 与另两者最显著的上下文一致性设计。

### 3.5 Tool、安全与 Effect

Lumen 把三个常被混淆的轴拆开：

| 轴 | 回答的问题 | 例子 |
| --- | --- | --- |
| `Risk` | 是否需要用户审批 | read / write / execute / external / external_unknown |
| `EffectKind` | 是否需要状态追踪、恢复和验证 | observe / mutation / execution / external_action / unknown |
| `ToolConcurrency` | 此次 invocation 能否并发 | exclusive / parallel_safe |

`ToolOutputSpec` 再把 canonical JSON result、模型文本和客户端 presentation 分开。Tool renderer 失败只产生
有界 diagnostic，不能把成功的 authoritative outcome 改成失败。`CapabilityGateway` 的 guard 只有
abstain/deny，任何 deny 都不能被后续 Hook 放宽。

文件访问通过 [`Workspace`](../../src/lumen/tools/workspace.py) 拒绝绝对路径、`..` 和 symlink escape；
mutation 使用 expected revision、no-replace create、替换前重验和原子发布。`run_command` 通过
[`SandboxRunner`](../../src/lumen/sandbox.py)；默认 `workspace_write` 在支持平台 fail closed，并终止超时/取消的
整个进程组。Approval、路径 confinement 和 OS sandbox 是三个正交机制。

### 3.6 TaskWorkspace：把“改了”升级为“可验证地改了”

[`TaskWorkspace`](../../src/lumen/work_products/workspace.py) 记录
`prepared → applied → verified/failed → rolled_back` effect journal。文本与结构化局部修改除了验证目标已变化，
还验证非目标区域/路径不变；恢复时把中断 effect 与当前资源 revision 对账。严格模式下，未验证 mutation、
未知 effect 或 reconciliation-required work product 会阻止 completion。

这是 Lumen 相对 Pi 和 DeepSeek Harness 最独特的 Module：后两者都有工具结果与文件安全机制，但默认产品 spine
没有等价的“工作对象 + effect verification + completion veto”组合。

### 3.7 多 Agent：线程权威与隔离导入

[`AgentOrchestrator`](../../src/lumen/agents/orchestrator.py) 是 Agent Thread、调度、消息、恢复、证据与完成门禁的
唯一权威；[`NativeAgentRuntimeFactory`](../../src/lumen/agents/runtime_factory.py) 只负责创建受限 child runtime 和
管理 worktree 结果。

V1 的能力约束是：

- child tools = 父轮次有效工具 ∩ role allow-list ∩ workspace mode；
- child 不获得多 Agent 控制工具，最大深度由能力层强制为一；
- explorer 共用工作区只读，worker/default 写入独立 Git worktree；
- 导入前检查基线、父工作区 dirty path 和三方冲突；
- active、未交付结果、未处理失败、待导入、冲突、缺 evidence、未验证导入都会阻止根 Agent 完成。

这是一种“受控并行工作产品”模型，而不是仅仅启动多个 loop。

## 4. Pi Agent：现役架构与 Harness v2 演进方向

### 4.1 必须区分两条实现路径

当前 Pi 源码有两层不能混写：

1. **现役经典路径**：`pi-ai → Agent / agentLoop → AgentSession → SessionManager → TUI/print/RPC/SDK`。
   CLI 主路径仍由 `createAgentSession()` 创建 `Agent`，`AgentSession` 监听事件并把消息写入 `SessionManager`。
2. **Harness v2 scaffold**：`packages/agent/src/harness/` 已包含新的 Session storage、record reducer、lane、
   compaction、skills、tools 和 telemetry Implementation，其中一部分类型已公开；coding-agent 也有
   `createCodingAgentHarness()` 装配器。但在固定
   commit 上，`prompt()`、`resume()`、`compact()`、`steer()`、`followUp()`、lanes、watch 与 hooks 等仍明确
   抛 `HarnessNotImplemented`，测试名称就是 `AgentHarness v2 scaffold`。

因此，本节的产品能力判断以经典路径为准；Harness v2 只作为演进信号。

证据：[`AgentHarness`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/harness/agent-harness.ts)、
[`scaffold test`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/test/harness/agent-harness-scaffold.test.ts)、
[`classic SDK construction`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/sdk.ts)。

### 4.2 pi-ai：Provider-neutral stream vocabulary

`pi-ai` 把 Provider/model metadata、统一 Message/Content、stream event、usage 与 retry 放在 Agent 之下。
`Agent` 只依赖一个 `StreamFn(model, context, options)`，因此 Anthropic、OpenAI、Google、Bedrock、DeepSeek 等
差异被限制在 Provider adapter。这个 seam 很小：Agent 不需要知道 HTTP/SSE wire format。

这是 Pi 最大的 Leverage：替换模型通常不改变 loop；应用也可以注入自己的 stream function。
证据：[`StreamFn`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/types.ts)、
[`pi-ai types`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/ai/src/types.ts)。

### 4.3 Agent 与 agentLoop

经典 `Agent` 直接拥有：

- `systemPrompt`、model、thinking level、tools、messages；
- `isStreaming`、partial assistant message、pending tool calls、error；
- steering 与 follow-up 两个内存队列；
- subscription listeners 与 active abort controller。

`runLoop()` 有内外两层循环：内层处理 tool continuation 与 steering，外层在本应结束时检查 follow-up。
每一步调用可选 `transformContext()`，再 `convertToLlm()`，然后通过 `StreamFn` 请求模型。assistant message 含
tool calls 时，loop 执行并追加 tool result；否则结束。

Pi 的语义非常直接：mutable `AgentMessage[]` 是运行中 transcript，事件主要服务 UI 和上层持久化。它没有
Lumen 的 Host/Coordinator/ContextEngine 三段提交，也没有 DeepSeek Harness 的 turn/step event log 先行。

工具并发是“批次级 + 工具 override”：默认 parallel，preflight 顺序执行，允许的工具并发执行，完成事件按
完成顺序发出，但最终 toolResult message 按 assistant 源顺序加入历史；任一工具声明 sequential 时整批回退。

证据：[`Agent`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/agent.ts)、
[`agentLoop`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/agent-loop.ts)。

### 4.4 AgentSession 与 SessionManager

coding-agent 的 `AgentSession` 是经典 core 与产品功能的胶合层：它处理 compaction、extension hook、session
event、tool lifecycle、model/tool 切换与 UI-facing state。`SessionManager` 把每个 entry 存成带 `id/parentId`
的 JSONL tree，active leaf 决定当前分支；`/tree` 可在同一文件内回到旧节点并形成新分支。

这个树模型对交互探索非常自然：历史分支不丢失，branch summarization 可以把离开的路径摘要注入新路径。
但 classic SessionManager 与 live `Agent.messages` 是两个需要 `AgentSession` 保持同步的表示；迁移旧 session 时
`migrateToCurrentVersion()` 可能重写整个文件。它不是 Lumen 的“旧 schema 永不重写”策略，也不是 DeepSeek
Harness 的 SessionEvent 单一模型历史源。

证据：[`SessionManager`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/session-manager.ts)、
[`AgentSession`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/agent-session.ts)、
[`sessions docs`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/docs/sessions.md)。

### 4.5 Compaction 与 branch summary

Pi 在 `contextTokens > contextWindow - reserveTokens` 时触发 compaction，向后保留约 `keepRecentTokens`，把旧历史
交给模型生成结构化 summary，并追加 `CompactionEntry(summary, firstKeptEntryId, tokensBefore, details)`。
完整原始 entries 仍在 JSONL tree 中；Provider 下次看到 summary + retained tail。

Pi 还处理 oversized single turn：cut point 不落在 tool result 上，必要时分别总结历史和当前 turn prefix。
默认 compaction 累积 read/modified file 列表；branch navigation 可总结离开的分支。

它的优点是简单、用户可理解、与 tree 强结合；弱点是 summary 主要是文本约定，没有 Lumen V2 checkpoint 的
parent/range/digest/evidence，也没有 DeepSeek Harness `compaction/start/end` durable lock bracket。

证据：[`compaction`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/compaction/compaction.ts)、
[`compaction docs`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/docs/compaction.md)。

### 4.6 Extensions 与安全边界

Pi 默认故意不内置 permission popups、Plan mode、subagents 或 sandbox；它们可以由 TypeScript extension/package
实现。Project trust 只决定是否加载项目本地 settings/resources/extensions，不限制模型之后让工具做什么。
内置工具与 extensions 以启动 Pi 的 OS 用户权限运行；官方建议在 container、VM、micro-VM 或外部 policy
sandbox 中运行不可信/无人值守任务。

这不是“没有安全意识”，而是明确选择不把 partial in-process sandbox 宣称为真实边界。代价是不同 extension
可以各自实现不同 approval/effect/recovery 语义，平台没有 Lumen 式统一 Risk/EffectKind/完成门禁，也没有
DeepSeek Harness 式统一 Tool registry waterfall。

证据：[`security.md`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/docs/security.md)、
[`coding-agent README`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/README.md)。

### 4.7 Harness v2 暗示的下一代方向

虽然尚未可运行，v2 scaffold 已显露明确方向：

- `Session` 与 storage Interface，支持 memory/JSONL backend；
- Entry tree + separate operation record log；
- 仓库内的 `reduceLaneState()` 从 durable slice 推导 active operation、tool batch、pending write、queue 和
  corruption；该 reducer 有测试，但未从 package 公共入口导出；
- lane 概念、多队列、manual/automatic drive、action stepping；
- record corruption 明确分类，restore 遇到不可能状态拒绝而非猜测修复。

这说明 Pi 正在尝试把经典路径中“live mutable Agent + 上层 SessionManager”的一部分一致性责任下沉到可恢复
harness。但固定 commit 上这些是 **部分已导出类型，以及仓库内已有测试的 scaffold，不是已完成的产品 loop**。

## 5. DeepSeek Harness：底层原理与技术架构

### 5.1 Cordis 组合根与 package 微内核

DeepSeek Harness 不是一个大 `AgentRuntime`，而是由 Cordis `Context` 装配的 package graph。核心 spine 只有：

```text
scope
  ↓
session → system-prompt → tools → agent(interface/registry) → agent-loop(implementation)
                           ↓
                         llm seam
```

Compaction、Approval、Sandbox、FS、Skills、Subagent、Jobs、Web、ACP 等都是可选 capability/plugin，而不是
硬塞进 loop。消费者依赖 `agent` Interface，不依赖 `agent-loop` 实现；factory 由 loop 注册到 AgentRegistry。

证据：[`core subsystem`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/core.md)、
[`core packages`](https://github.com/deepseek-ai/deepseek-harness/tree/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core)。

### 5.2 Scope：一次注册同时表达可见性与生命周期

`ScopeKey` 是不透明对象 identity，生产 loop 直接以 live `Agent` 对象作为 key。通过 `agent.ctx` 注册的 Tool、
Prompt、listener 或 restriction 只在该 Agent scope 可见，并在 scope dispose 时按 effect ownership 清理。
`ScopedLayers` 提供 global layer + exact-scope overlay，scope-local 条目 shadow global，读操作不创建 layer。

这同时解决两个常被拆开的事情：

- **谁能看到这个能力**；
- **谁销毁时必须撤销这个能力**。

Lumen 的 `RegistrationScope` 已解决可逆注册与 task quiescence，但能力可见性主要由 Registry/Factory 快照和
child tool intersection 处理；DeepSeek 的 scope 更统一，也更依赖插件框架。

证据：[`scope`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/scope.md)、
[`scope source`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/scope/src/index.ts)。

### 5.3 ReactLoopAgent：每个请求从 Session log 派生

`ReactLoopAgent` 的 phase 只有 idle / maintenance / running，public status 只暴露 idle/running。Inbox 有
`next-turn` 与 `next-step` 两个可由 Session 记录重建的 projection；followup/steer/inject 是 `send()` 的固定
preset。

一轮执行：

1. append `turn/start`；
2. claim inbox，assemble scoped prompt 与 dynamic runtime context；
3. `agent/pre-step` waterfall 决定 reject 或 enter；
4. append `step/start` 与 user messages；
5. 从 `session.deriveMessages()` 构建模型请求，stream chunk 全部先 append；
6. block assembler 生成 assistant message 并 append；
7. 有 tool call 则调统一 ToolRuntime，把 result additional context 插回 next-step inbox；
8. append `step/end`、`turn/end`；失败、abort、max-token 都有 typed reason。

与 Pi 最大差别是：Pi 的 loop 先操作 mutable messages 再发事件；DeepSeek Harness 的 loop 把 turn/step/message/tool
事实持续写入 Session log，下一请求重新 derive。与 Lumen 最大差别是：Lumen 的 journal 以 terminal turn 为主，
Provider streaming 通过 EventJournal 与 partial outcome 持久化；DeepSeek 把更细的 step/chunk 直接作为 Session 事实。

证据：[`ReactLoopAgent`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/agent.ts)。

### 5.4 Session event sourcing 与 surface

Session 是 append-only typed `SessionEvent[]`，LLM history 由 `deriveMessages()` 派生，不维护第二份独立 message
history。事件除了 log 顺序，还有 **surface**：message-producing event 可 append 或 replace 已有 surface range，
因此 compaction、tool-result pruning 和动态上下文 snapshot 能保留原始 log，同时改变模型下次看到的投影。

Persistence 是独立 Interface。事件先同步进入 session，backend controller write-behind batching；
`session/flush` 取消等待并 drain 至 quiescence，是下一 ordinary turn 前的 durability checkpoint。冷恢复遇到中断
turn 会保留已提交事实并补 synthetic balance event，而不是静默截断物理 tail。

证据：[`session`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/session.md)、
[`persistence`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/persistence.md)。

### 5.5 Tool pipeline 与确定性并发

ToolRuntime 把工具定义、scoped visibility、限制、guard、policy waterfall、around dispatch、post decision、
presentation 和 final notification 收口到一个 pipeline：

```text
resolve visible definition
  → validate / classify execution mode
  → tools/pre-execute (allow / deny / ask)
  → monotonic guards
  → tools/execute around waterfall
  → body
  → tools/post-execute
  → finalize content/meta
  → frozen result + tools/result
```

并行调度比 Pi 更细：exclusive 调用形成 barrier；parallel 调用进入 bounded rolling pool；每个未启动 call 在启动前
重新分类，因此运行中 registry 变化可以建立新 barrier。实际 dispatch 可并行，但 pre-policy、Session result 与
additional context 按模型源顺序提交；是否已经 durable 取决于 persistence controller 与 flush barrier。Abort 停止补充新任务、drain 已启动调用，并为未启动 call 写 synthetic
aborted result，使 replay 仍平衡。

证据：[`tools`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/tools.md)、
[`tool scheduler`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/tool-calls.ts)。

### 5.6 Compaction 是可选 capability，不是 loop 内建分支

Compaction 通过 `compaction/start`、`summary`、`end` 记录完整 durable bracket。summary 本身以一个
`user/message` surface replacement 替换选定范围；旧事件仍在 log。`start` 先写、`end` 最后写，使中途崩溃
表现为 orphaned lock，不会误称操作完成。范围选择保持 tool call/result pairing，可先调用 deterministic
tool-result pruner 再决定是否需要模型摘要。

这比 Pi 的单个 CompactionEntry 更事务化；与 Lumen 相比，它以 surface replacement generation 为中心，Lumen
则以 source transcript checkpoint chain、digest、Artifact receipt 与两阶段发布为中心。

证据：[`compaction`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/compaction.md)。

### 5.7 Approval 与 Sandbox

Approval 的 outcome 是闭合集合，只有 `allowed-once` 放行；缺少 answerer 或异常默认 unavailable/deny。
Session policy 当前主要是 `ask/never`，每次 asked/decided 都是 log-only audit pair。这一设计 fail closed，但策略
表达力低于 Lumen 的 Risk、manual/auto/plan/accept-edits 和 once/session/always scope。

Sandbox mode 是 `read-only / workspace-write / danger-full-access`，只承诺文件 effect；network 与 process
visibility 明确不在 vocabulary 内。Provider 返回 `full/partial` enforcement，旧 Landlock ABI 或 Windows ACL
的残缺能力不会被伪装成 full；没有可用 confined backend 必须 `SANDBOX_UNAVAILABLE`，不得 silent passthrough。

这对 Lumen 最直接的启发是：**mode 是 policy intent，enforcement 是运行事实**，两者都应出现在 capability
report 和审计中。

证据：[`approval`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/approval.md)、
[`sandbox`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/sandbox.md)。

### 5.8 Subagent、continuable child 与 Agent Teams

`ctx.subagents` 是 named Provider registry，可以同时存在 in-process spawn/fork、ACP、Codex、Claude Code、
DSH SDK 等 backend。Start 前先检查 output schema、depth limit、tool filter、persona 等 capability，不能把不支持
的选项接受后忽略。

one-shot child 返回 `SubagentRun`；continuable child 则以 child SessionId 为 durable identity，由 Activation
manager 负责 resident/cold resume，Agent inbox 是唯一 FIFO。Delegation depth 同时写 Session header 与 runtime
option，恢复不能把 child 伪装回 root。Fork 只复制到父级最后一个已闭合 `turn/end` 的平衡前缀。

实验性 Agent Teams 又提供 implicit root、durable roster、queued-minus-delivered mailbox 和 shared task DAG；
这是比 Lumen V1 depth-one 更宽的拓扑。但其 shared checkout 与通用 child provider 模型没有 Lumen 当前的
isolated worktree import/reject、父 dirty-path guard、Plan evidence 和 TaskWorkspace completion gate。

证据：[`subagent`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/subagent.md)、
[`agent teams`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems/agent-team.md)。

## 6. 逐层对比矩阵

| 维度 | Lumen | Pi 经典路径 | DeepSeek Harness |
| --- | --- | --- | --- |
| 首要定位 | 通用、可配置、对安全/恢复/工作对象负责的 Agent framework | 极简、可嵌入、多 Provider coding harness | 可组合、可热插拔的 Agent 产品微内核 |
| 语言/运行时 | Python 3.11–3.13，Pydantic AI loop | TypeScript/Node，pi-ai stream | TypeScript/Node，Cordis plugin context |
| loop 权威 | `AgentRuntime` | `Agent` / `agentLoop` | package-private `ReactLoopAgent` |
| turn 协调 | `RunCoordinator` | `AgentSession` | Session turn/step events + driver phase |
| 客户端 seam | `WorkspaceHost` command/event | AgentSession events；各 mode 直接组合 | Agent public handle + host/client plugins |
| 运行中 history | ContextEngine active projection | mutable `Agent.messages` | `Session.deriveMessages()` |
| durable history | v9 append-only JSONL records | JSONL tree entries | typed append-only SessionEvent 内存 log + 独立 persistence seam |
| 分支 | Session fork；不以内文件 message tree 为主 | 单 JSONL 文件原生 parentId tree | seed/fork event prefix；surface replacement |
| 压缩 | Zone budget + Artifact receipt + V2 checkpoint chain | text summary + retained tail + branch summary | optional capability + durable bracket + surface replace |
| 工具权限 | Risk + approval modes/rules | 默认无 approval；extension 自定义 | pre-execute ask/deny + approval service |
| 工具副作用 | EffectKind + TaskWorkspace journal | tool result；无统一 effect authority | Session tool call/result + persistence barrier；无通用 Work Product gate |
| 并发 | per-invocation concurrency，默认 exclusive | batch parallel/sequential | live reclassification + barriers + bounded pool |
| Sandbox | workspace_write fail closed；路径层另行限制 | 无内建 sandbox，推荐外部隔离 | per-call file sandbox，报告 full/partial |
| 多 Agent | depth-one、独立 worktree、显式 import/reject | 默认不内置；extension/package | named providers、one-shot/continuable、teams |
| Completion gate | Plan + Work Product + Agent unresolved state | 无系统级联合 gate | turn terminal + capability-specific quiescence |
| 恢复策略 | schema v1–v9；partial outcome、receipt、reconcile | classic session reload；v2 在设计 reducer/recovery | interrupted turn balance + flush/persistence preparation |
| 扩展生命周期 | Registry + RegistrationScope + ResourceManager | arbitrary TS extension，以进程权限运行 | scope + Cordis disposer/HMR/rollback |
| 抽象成本 | 中高；深 Module 少但 Interface 已较宽 | 低；产品策略由扩展承担 | 很高；package graph 与 waterfall 调试复杂 |

## 7. 状态权威：三者最本质的差异

### 7.1 Pi：以 live Agent 为中心

经典 Pi 的最短理解路径是 `Agent.state.messages`。Session persistence 是 coding-agent 上层通过 event bridge 维护的
产品能力。优点是 library 使用者可以直接嵌入 Agent；风险是更高级产品必须自己确保 Agent state、Session tree、
UI、extension state 在失败时一致。

### 7.2 DeepSeek Harness：以 Session event log 为中心

模型 history、inbox projection、turn/step、tool result、compaction surface、approval audit 都围绕 Session event。
Loop 是 log 的写入者与派生消费者。好处是 replay 和插件 projection 有共同语义；代价是 event vocabulary、
surface rule、scope 和 listener ordering 成为全系统必须掌握的基础设施。

### 7.3 Lumen：按状态概念分权，Session 保存持久事实

Lumen 不把所有运行时状态压进 SessionRepository。Session 只追加事实，不调度；Host、Coordinator、Runtime、
ContextEngine、TaskWorkspace、Orchestrator 分别拥有一个状态概念。好处是领域 Locality 更强；风险是跨 Module
提交顺序必须有明确协议，否则容易产生“某权威已前进、另一个尚未持久化”的裂缝。

Lumen 当前用 `prepare/commit/fsync/confirm`、typed record 和 completion gate 解决这些裂缝。后续重构应优化
Interface 面积，而不是为了统一外形把所有权威迁入一个通用 event bus。

## 8. 安全与可信完成的对比

### 8.1 Pi：明确的本地信任模型

Pi 的安全承诺最小但诚实：默认工具和 extension 就是当前用户权限。适合受信本地仓库、交互式监督和外部容器。
不适合在没有额外封装时直接承担企业策略、无人值守外部操作和可恢复副作用。

### 8.2 DeepSeek Harness：能力管线强，边界事实细

DeepSeek Harness 的 Tool waterfall、monotonic guard、approval fail-closed、Sandbox full/partial 和 FS freshness
语义很强。但 Cordis plugin 仍是可信同进程代码，scope 不是恶意插件 sandbox；默认 sandbox vocabulary 只覆盖
file effect，不能把它描述成完整主机隔离。

### 8.3 Lumen：风险、效果、验证和完成形成闭环

Lumen 的优势不只在“有 sandbox”，而在：

```text
tool intent
  → Risk/approval
  → path confinement + OS sandbox
  → EffectKind journal
  → post-state verification / reconciliation
  → completion gate
```

这个闭环应继续作为产品差异化，不应为了兼容外部 Agent backend 而旁路。

## 9. 多 Agent 的两种不同问题

多 Agent 实际包含两个问题：

1. **怎么并行运行多个模型 loop？** DeepSeek 的 Provider/Activation 和 Pi extension 都能解决。
2. **并行修改怎样安全进入主工作区并成为可声明完成的证据？** Lumen 的 worktree + import verification +
   completion gate 更直接地解决。

因此，如果 Lumen 将来支持 DeepSeek/Codex/Claude Code 作为 child backend，推荐结构是：

```text
AgentOrchestrator (identity / ownership / status / evidence / completion)
  └─ AgentRuntimeFactory backend
       ├─ native Lumen runtime
       ├─ DeepSeek Harness adapter
       └─ external ACP / Codex / Claude adapter

All writable results
  → isolated workspace/worktree
  → import preflight
  → TaskWorkspace effect journal
  → verification
```

外部 backend 可以拥有 child loop，但不能拥有 Lumen Agent Thread 的最终状态、导入决策或完成资格。

## 10. 优势、局限与适用场景

### 10.1 Lumen

**优势**

- 唯一权威和依赖方向清楚，TUI/Web/headless 共享 Host seam。
- Context、Work Product、Agent 生命周期都是有完成门禁的深 Module。
- Session 长期兼容、ArtifactRef、request receipt 和恢复语义适合严肃工程任务。
- Risk、Effect、Concurrency 分轴，安全与可验证 mutation 不是工具名硬编码。

**局限**

- `WorkspaceHost`、`AgentRuntime.run()`、`SessionRepository` Interface 面积持续增长，认知成本已明显高于 Pi。
- 多个深 Module 的提交协议正确但复杂，需要更多状态机/property/conformance tests 防止跨 Module 漂移。
- 以 Pydantic AI 为底层 loop Implementation，某些 streaming/tool 语义需要 Adapter 翻译，调试链更长。
- 原生多 Agent V1 深度和 backend breadth 小于 DeepSeek Harness。

**适用**：需要跨客户端一致行为、强审计、长期 Session、受控工作区 mutation、可恢复多 Agent 的应用框架。

### 10.2 Pi

**优势**

- loop 小、源码直观、事件顺序清楚，适合研究和嵌入。
- Provider breadth 和 stream abstraction 成熟。
- JSONL tree、branch navigation、HTML export 与 extension UX 很适合个人 coding workflow。
- 默认不强加 Plan/approval/subagent 产品哲学，定制自由度高。

**局限**

- 无内建 sandbox/approval/completion gate，extension 语义可能碎片化。
- 经典 live Agent 与 SessionManager 是需要胶合层同步的两种表示。
- session migration 可重写文件，不适合直接承诺 Lumen 式长期 append-only schema chain。
- Harness v2 虽方向先进，但当前不能当作已实现恢复 runtime。

**适用**：受信本地环境、需要快速二次开发、多 Provider、偏个人/团队工具而非平台级副作用治理。

### 10.3 DeepSeek Harness

**优势**

- Session event sourcing、scope、registry、waterfall 与 HMR 生命周期高度一致。
- Loop Interface 与 Implementation 分离真实，LLM/FS/Sandbox/Persistence/Subagent 都有多个实现。
- Tool scheduler、cancel/drain、确定性 Session 提交顺序和 request reconstruction 很严谨。
- Subagent provider breadth、continuable child、teams、Code Mode 等 orchestration 面广。
- 文档生成、invariant companion、composition test 和 per-package contract 治理强。

**局限**

- 仍是 RC，公开 Interface 与持久化格式不宜当成稳定行业标准。
- package graph 极大，有效行为可由 scope、config 和 waterfall listener 动态改变，定位问题难度高。
- 插件仍是同进程可信代码；Sandbox file-effect vocabulary 不覆盖完整网络/进程边界。
- 缺少 Lumen 式统一 Work Product/effect verification/completion veto。
- 架构 Leverage 高，但小团队复制其微包粒度很容易得到 pass-through Module 和维护负担。

**适用**：需要多产品形态、多 backend、热装卸和精细事件语义，且能承担框架复杂度的 Agent 平台。

## 11. 对 Lumen 的分阶段建议

### P0：保持不动的核心不变量

1. 保持 `WorkspaceHost → Coordinator/Runtime → Context/Tools/Session` 依赖方向。
2. 保持 Session append-only 与 v1–v9 不重写历史。
3. 保持 Risk / EffectKind / ToolConcurrency 三轴独立。
4. 保持 TaskWorkspace 和 AgentOrchestrator 对 completion 的 veto 权。
5. 外部 Provider、Skill、Plugin、child role 只能收窄权限，不能扩大父级 sandbox。

### P1：低风险、高收益

1. 在 capability report 增加 sandbox backend、`full/partial/unavailable`、network/process visibility 字段。
2. 为 `AgentRuntime` 建一个类似 Pi 的最小 loop sequence 文档/契约测试，固定 text/tool/steer/follow-up/cancel
   的事件顺序，降低读完整 `run()` 的必要性。
3. 借鉴 DeepSeek request reconstruction invariant，把 request receipt 与 Session history/tool digest 的可重建性做
   property test，而不只验证字段存在。
4. 为 Agent/Work Product/Context 的跨 Module 提交增加 fault-injection matrix：在 prepare、tool applied、terminal
   append、fsync、confirm、import finalize 每个点模拟崩溃。

### P2：有第二实现时再抽象

1. 只有接入第一个真实外部 child backend 时，才把 `NativeAgentRuntimeFactory` 收敛成 provider registry；
   `AgentOrchestrator` 不变。
2. 只有出现第二个 Session persistence backend 时，才引入更公开的 persistence Interface；不要提前复制
   DeepSeek 的 package seam。
3. 只有 TUI/Web/外部 SDK 对 scoped dynamic capabilities 出现重复逻辑时，才扩展 `RegistrationScope` 的
   visibility 语义。

### P3：暂不建议

- 不把整个系统迁到 LangGraph 或另一个 checkpoint graph；会制造第二状态权威。
- 不复制 DeepSeek 的微包数量和所有 waterfall；先用 deletion test 证明每个新 Interface 有两个真实实现或能
  消除多处复杂度。
- 不把 Pi extension 的任意进程权限模型作为 Lumen plugin 默认。
- 不把多 Agent depth 放宽当作目标本身；先证明 depth-one 在任务图上形成真实瓶颈，并为 nested ownership、
  approval routing、worktree forest 和 completion evidence 定义协议。

## 12. 最终判断

从底层原理看，三者分别优化不同目标：

- Pi 优化 **最短的模型/工具反馈环与最大定制自由**；
- DeepSeek Harness 优化 **事件事实、插件组合、作用域和实现替换**；
- Lumen 优化 **应用级唯一权威、可恢复副作用、工作对象验证与可信完成**。

Lumen 当前不需要证明自己比 Pi 更小，也不需要证明比 DeepSeek Harness 拥有更多 seam。更合理的路线是：
保留少数真正有 Depth 的 Module，把 Provider/Tool/child backend 做成可替换 Implementation；把 capability
visibility、sandbox enforcement、request reconstruction 和 fault-injection 做得更机械可验；继续让 Session、
TaskWorkspace 与 AgentOrchestrator 对“发生了什么”和“是否真的完成”拥有最后解释权。

## 13. 一手源码索引

逐项固定对象、关键符号与冲突复核见
[`上游源码证据账本`](./upstream-pi-deepseek-harness-sources.md)。

### Lumen 本地

- [`WorkspaceHost`](../../src/lumen/application/host.py)
- [`RunCoordinator`](../../src/lumen/run_coordinator.py)
- [`AgentRuntime`](../../src/lumen/runtime.py)
- [`ContextEngine`](../../src/lumen/context/engine.py)
- [`SessionRepository`](../../src/lumen/sessions.py)
- [`TaskWorkspace`](../../src/lumen/work_products/workspace.py)
- [`AgentOrchestrator`](../../src/lumen/agents/orchestrator.py)
- [`NativeAgentRuntimeFactory`](../../src/lumen/agents/runtime_factory.py)
- [`ToolRegistry`](../../src/lumen/tools/registry.py) 与
  [`CapabilityGateway`](../../src/lumen/tools/gateway.py)

### Pi 固定 commit

- [`Agent`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/agent.ts)
- [`agentLoop`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/agent-loop.ts)
- [`AgentSession`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/agent-session.ts)
- [`SessionManager`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/coding-agent/src/core/session-manager.ts)
- [`AgentHarness v2 scaffold`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/harness/agent-harness.ts)
- [`reduceLaneState reducer`](https://github.com/earendil-works/pi-mono/blob/a470b121bf683b4c2b9fc0b3a7c807de7e0cfe9c/packages/agent/src/harness/reducer.ts)

### DeepSeek Harness 固定 commit

- [`Agent Interface`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent/src/types.ts)
- [`ReactLoopAgent`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/agent.ts)
- [`Tool scheduler`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/agent-loop/src/tool-calls.ts)
- [`Session`](https://github.com/deepseek-ai/deepseek-harness/tree/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/session)
- [`ToolRuntime`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/tools/src/index.ts)
- [`Scope`](https://github.com/deepseek-ai/deepseek-harness/blob/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/packages/core/scope/src/index.ts)
- [`Subsystem docs`](https://github.com/deepseek-ai/deepseek-harness/tree/b150a551b8d465e31e418e1b2eaf5e79bbb7d28e/docs/subsystems)
