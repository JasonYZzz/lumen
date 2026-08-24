# Pi、Claude Code 与 OpenAI Codex Agent 架构研究

研究日期：2026-08-04  
研究范围：外部 coding agent 的公开架构；仅使用官方文档、官方博客和官方开源仓库。  
证据边界：Pi 与 Codex 的核心 harness 有公开源码；Claude Code 核心 harness 未公开源码，因此 Claude Code 只能比较官方文档承诺的行为，不能把产品行为反推成确定的内部类、状态机或调度算法。

## 1. 名称与结论边界

本文把用户所说的 **pi** 解释为 Pi coding agent，即原 `badlogic/pi-mono`、当前官方仓库中的 terminal coding harness。依据是其官方 README 直接称其为 “minimal terminal coding harness”，且仓库包含 `pi-agent-core` 和 `pi-coding-agent`。[Pi coding agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)

需要先区分三种经常被混称为“运行模式”的东西：

1. **Agent loop**：模型输出工具调用，harness 执行工具，把结果加入上下文，再调用模型，直到模型输出最终消息。它最接近 ReAct，但产品通常不会把每一步显式命名为 Thought/Action/Observation。
2. **协作/权限模式**：例如 Plan、Default、Auto、read-only、accept edits。它主要改变提示词、可用工具或审批策略，不必然代表换了一套 loop。
3. **部署/交互模式**：interactive、print、RPC、SDK，或 local、worktree、cloud。它改变入口和执行环境，不等于改变推理算法。

公开资料没有显示 Pi、Claude Code 或 Codex 在每个普通请求前都运行一个独立的、确定性的“意图分类器 → DAG planner → executor”。更符合证据的描述是：**模型在同一个迭代式 tool loop 中，根据用户输入、项目说明、对话历史、可用工具 schema、权限/环境以及每次工具返回动态决定下一步**。显式 Plan mode 是在这个基础上增加只读约束、专用指令或人工批准点。此句是基于下述公开 loop 与 mode 行为作出的架构归纳，不是三家公司共同发布的内部实现声明。

## 2. 三者的运行循环与规划依据

### 2.1 Pi：最小、透明、由模型驱动的 tool loop

Pi 的内置 loop 可以直接从源码确认：加入用户消息，流式请求模型；若 assistant message 包含 tool calls，则校验并执行工具、追加 tool results，然后再次请求模型；若没有 tool call 和排队消息则结束。一个 assistant message 中的工具可按配置串行或并行执行，默认 loop API 支持 parallel；用户在运行中输入的 steering message 会在当前工具批次后注入，follow-up message 则在 agent 原本要停止后继续外层循环。[Pi `agent-loop.ts`](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts) [Pi agent types](https://github.com/earendil-works/pi/blob/main/packages/agent/src/types.ts)

Pi coding agent 默认只给模型四个工具：`read`、`write`、`edit`、`bash`。官方明确说明 Pi **刻意不内置** subagents、plan mode、permission popups、todos 和 background bash；这些能力由 TypeScript extensions 或第三方 packages 实现。[Pi README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md) [Pi usage docs](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md)

因此 Pi 的任务规划依据是：system/context files、用户消息、当前消息树分支、工具定义与工具结果，以及所选模型自身的推理能力。原生没有可证明的 plan-and-execute controller；“先规划再执行”只能来自用户提示词、模型自行列计划，或 extension 注入的工具、命令、hook 和状态。这个判断是源码与官方“skips plan mode”的直接结果。

Pi 的上下文与持久化很有特色：session 是带 `id`/`parentId` 的 JSONL 树，可在单文件内分叉和回到旧节点；长上下文默认自动压缩，也可 `/compact`。压缩会保留最近消息，LLM 总结较老消息，并把 summary、`firstKeptEntryId`、已读/已修改文件等写回 session；完整原始历史仍留在 JSONL 中。分支切换还可单独总结被离开的分支。[Pi sessions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sessions.md) [Pi compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)

### 2.2 Claude Code：自适应 gather → act → verify loop，Plan 是受限阶段

Anthropic 对 Claude Code 的官方抽象是 **gather context → take action → verify results → repeat**。三阶段会混合，模型根据用户请求和上一步观察选择文件、搜索、shell、web、code intelligence 等工具；提问可能只 gather，bug fix 会反复执行三阶段。用户可以随时中断和纠偏。[How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)

Plan mode 让 Claude 读取文件、运行探索命令并提出计划，但不修改源文件；计划提交给用户后，用户可选择不同执行权限继续，也可要求继续规划。Anthropic 的建议是只在路径不确定、跨文件或陌生代码等复杂任务上 plan；小而明确的改动直接执行。官方推荐的复杂任务工作流是 Explore → Plan → Implement → Commit。[Permission modes](https://code.claude.com/docs/en/permission-modes) [Claude Code best practices](https://code.claude.com/docs/en/best-practices)

因此公开证据支持“同一自适应 loop 在 Plan 权限/指令下探索并产出计划”，但不支持断言 Claude Code 内部一定有独立 Planner 模型、固定 DAG 或独立 Executor 类。`opusplan` 可以在 plan 阶段用 Opus、执行阶段用 Sonnet，说明产品可按阶段切模型；这仍不等价于公开了一套符号规划算法。[Claude Code model configuration](https://code.claude.com/docs/en/model-config)

Claude Code 的规划输入还包括：启动时加载的 `CLAUDE.md`、按路径懒加载的嵌套说明、auto memory、对话和工具输出、skills/MCP 工具，以及用户给出的验收目标。Auto memory 按项目保存为 Markdown，`MEMORY.md` 的有限前缀在新会话启动时加载，topic files 按需读取。[Claude Code memory](https://code.claude.com/docs/en/memory)

上下文接近上限时，Claude Code 会先清理旧工具输出，再总结对话；`/compact` 可手动触发并附带聚焦要求，`PreCompact`/`PostCompact` hook 可参与生命周期。项目根 `CLAUDE.md` 会在压缩后重新注入，早期仅存在于对话中的细节可能丢失。[How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works) [Agent SDK loop and compaction](https://code.claude.com/docs/en/agent-sdk/agent-loop)

### 2.3 Codex：Responses API 驱动的公开 loop + thread runtime

OpenAI 对 Codex loop 的公开描述非常具体：用户输入进入 prompt；模型若返回 tool call，harness 执行并把结果追加到 input 后重新推理；当模型不再调用工具而返回 assistant message 时，本 turn 结束。历史消息和工具调用会进入后续 turn；Codex CLI 通过 Responses API 推理，初始请求由 model instructions、工具 definitions、用户/项目/环境输入共同构成。[Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)

Codex 的工具来自三层：Codex harness 内置工具、Responses API 提供的工具、用户通过 MCP 等提供的工具。官方特别指出 Codex 的 shell sandbox 只约束 Codex 提供的 shell；MCP 工具必须执行自己的 guardrails。这意味着“工具可调用”与“工具在同一安全边界内”不能混为一谈。[Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)

Codex 的 Plan 是 collaboration mode preset：App Server 可列出 mode，Plan 使用专用 built-in developer instructions，并有 Plan 专属 reasoning effort 默认值；计划作为独立 `plan` item 流式输出。公开接口说明了 mode 是覆盖在基础设置之上的 preset，而不是另起一套外部 planner service。[Codex App Server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) [Codex MCP interface](https://github.com/openai/codex/blob/main/codex-rs/docs/codex_mcp_interface.md)

Codex Core 除 loop 外还负责 thread 的创建、恢复、分叉、归档和事件持久化，以及 config/auth、sandboxed tool execution、MCP 和 skills。App Server 是长生命周期、双向 JSON-RPC/JSONL 进程，thread manager 为每个 thread 启动一个 core session；客户端可接收 reasoning、command、diff、plan 等流式事件，也可在 approval 时由 server 反向请求客户端并暂停 turn。[Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness/)

Codex 在 token 达到阈值时自动 compact。当前公开说明是通过 Responses API compaction endpoint 把原 input 替换成更小的 items，其中可包含带 opaque encrypted content 的 compaction item，用于保留模型对原会话的潜在理解。这比纯文本 summary 更深地绑定 OpenAI Responses API，也更难由外部审计或跨模型迁移。[Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/) [Codex `compact.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs)

Codex 的 `AGENTS.md` 从 global scope 和 project root 到 cwd 分层拼接，越近的说明越晚出现；默认项目说明总量有大小上限。它们与用户 prompt、历史、工具 schema、sandbox/approval 说明和 environment context 一起构成模型规划依据。[Codex AGENTS.md docs](https://learn.chatgpt.com/docs/agent-configuration/agents-md) [Codex `agents_md.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs)

## 3. 子代理、并行与状态隔离

| 维度 | Pi | Claude Code | Codex |
|---|---|---|---|
| 默认子代理 | 不内置；extension/package 自行实现 | 内置 subagent；独立 context，结果总结回主会话 | 内置 subagent workflow；独立 agent thread，主 thread 汇总 |
| 原生并行 | 单次 assistant message 的独立 tool calls 可并行；多代理非原生 | background subagents 可并发；agent teams 为多个独立 Claude Code 实例 | 多个 subagent threads 并行；可查看、steer、interrupt、wait、close |
| 上下文继承 | 由 extension 决定 | subagent 接收主 agent 编写的 delegation prompt，不继承完整主历史；加载指定 prompt/tools/skills。subagent 不能再 spawn subagent | 子 agent 可 fork 一定范围历史或接收新任务；custom agent 可覆盖 model、reasoning、instructions，其他 session 设置按规则继承 |
| 协作拓扑 | 无内置拓扑 | 普通 subagent 是 caller → worker → summary；实验性 agent teams 另有 lead、teammates、shared task list、mailbox | parent/child agent threads；主 thread 负责 spawn、follow-up、wait 和 synthesis |
| 写冲突处理 | 由用户/extension/外部 worktree 负责 | agent teams 文档明确提示同文件并行修改风险；可用 worktree 隔离会话 | 官方建议优先并行 read-heavy 工作；write-heavy 需谨慎；Desktop 原生支持不同 worktree |

Claude Code 普通 subagent 启动时拥有新 context window，可前台阻塞或后台并发；后台 agent 遇到需要新审批的调用会自动拒绝。普通 subagent 不能嵌套 spawn。另一个独立的实验性 **agent teams** 架构使用 team lead、多个完整 Claude Code 实例、shared task list 与 mailbox，teammate 能彼此直接通信，但 token 成本和协调开销明显更高。[Claude Code subagents](https://code.claude.com/docs/en/sub-agents) [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams)

Codex 当前本地版本默认提供 subagent workflows；agent 活动作为独立 thread 可被检查，parent 可 follow up、等待和停止。官方建议将探索、测试、triage、summary 等 read-heavy 工作并行化，并警告并行写会带来冲突。子 agent 继承父 turn 的 sandbox 与 permission mode，custom agent 可进一步设为 read-only。[Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

## 4. 权限、安全与执行环境

| 维度 | Pi | Claude Code | Codex |
|---|---|---|---|
| 内建 sandbox | **无**。进程和 extensions 使用启动用户权限 | 有。Bash/子进程使用 macOS Seatbelt、Linux/WSL2 bubblewrap；内置文件工具另走 permissions | 有。local 使用 OS-level sandbox；cloud 使用隔离容器 |
| 权限层 | project trust 只控制是否加载项目 extension/settings，不限制模型后续工具行为 | allow/ask/deny rules + permission modes；与 sandbox 形成两层控制 | sandbox mode + approval policy + 可选 reviewer；与 tool/MCP side-effect annotations 结合 |
| 默认边界 | 本机用户权限；官方建议把整个 Pi 放入 container/VM/micro-VM | 默认 mode 读无需批准；其他行为依 mode/rules；sandbox 可限制 workspace 和域名 | 默认 network off、workspace write；越界或联网按 policy 请求 approval |
| 执行形态 | interactive、print/JSON、RPC、SDK；本机为主，可外部容器化 | local、Anthropic cloud VM、Remote Control；CLI/Desktop/IDE/web/CI | local、Git worktree、cloud；CLI/Desktop/IDE/web，统一 harness/App Server |
| 扩展代码风险 | extensions/packages 是任意 TS，拥有完整进程权限 | hooks、plugins、MCP 分别受配置与权限规则影响；MCP/外部进程仍需各自安全评估 | Codex shell sandbox 不自动包住 MCP；MCP/app 依其 annotations 和自身 guardrails |

Pi 官方明确说 project trust 不是 sandbox；内置工具、extension、package manager 和开发工具均以 Pi 进程权限运行。其安全哲学是不要用“部分 in-process sandbox”制造错误安全感，而是把整个 Pi 或 tool execution 放入真实 OS/container/VM 边界。[Pi security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md) [Pi containerization](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md)

Claude Code 把 permission rules 与 OS sandbox 分成互补层：permissions 决定工具/文件/域可否尝试，sandbox 对 Bash 及子进程做 OS 级强制；macOS 用 Seatbelt、Linux/WSL2 用 bubblewrap。其 local、cloud、Remote Control 共享 agentic loop，但代码运行地点不同。[Claude Code permissions](https://code.claude.com/docs/en/permissions) [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing) [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)

Codex 同样分离 sandbox 与 approvals：前者定义技术边界，后者定义何时停下。local 默认 workspace-write/no-network；cloud 是两阶段环境，setup 可按配置联网安装依赖，agent phase 默认离线，且 setup secrets 在 agent phase 前移除。[Codex sandbox](https://learn.chatgpt.com/docs/sandboxing) [Codex approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security)

## 5. 总体架构对比表

| 维度 | Pi | Claude Code | OpenAI Codex |
|---|---|---|---|
| 核心定位 | 可嵌入、可改造的最小 harness | 完整 coding-agent 产品与工作流平台 | 开源 Rust harness + 多客户端/App Server + cloud runtime |
| 核心 loop | 公开 TS；model → tools → results → model | 官方行为为 gather → act → verify；核心实现闭源 | 公开 Rust；Responses API → tools/results → Responses API |
| 规划方式 | 默认无 Plan；靠模型、prompt、extension | 明确 Plan 权限模式；探索、计划、人工批准后执行 | Plan collaboration preset；专用 instructions/reasoning，plan item 流式返回 |
| 模式与 loop 关系 | interactive/print/RPC/SDK 是入口；不改变基础 loop | permission/plan 改变允许动作；基础 agentic loop 延续 | collaboration/sandbox/approval 是 turn settings；基础 Codex loop 延续 |
| 默认工具面 | 极小：read/write/edit/bash | 文件、搜索、shell、web、code intelligence、Agent 等 | shell/file、Responses tools、MCP、skills/plugins，且可 tool search |
| Session 模型 | JSONL message tree，原生分叉/回退 | 本地 transcript，可 resume/rewind；auto memory 独立持久化 | thread lifecycle/event history；App Server 创建/恢复/分叉/归档 |
| Context 策略 | 文本 summary + 最近消息；原历史保留；branch summary | 清旧 tool outputs + summary；CLAUDE.md 重注入；skills/MCP 延迟加载 | token threshold compaction；可用 opaque encrypted compaction item；强调 prompt caching |
| 多模型/供应商 | 多 provider 是核心卖点 | Anthropic 模型为中心，也支持 Bedrock/Vertex/Foundry 部署 | OpenAI Responses API 为中心，也允许兼容 endpoint/OSS provider |
| 可观测性 | event stream、JSON mode、完整 JSONL tree，源码小而直观 | 产品 UI、transcript、hooks、OpenTelemetry；内部 loop 不可源码审计 | 细粒度 event stream、App Server protocol、开源 core、cloud/desktop UI |
| 扩展哲学 | extension 可以替换几乎一切，核心保持小 | skills + hooks + MCP + plugins + subagents，平台内建约束多 | skills/plugins/MCP + App Server/SDK；统一 policy 和 thread 语义 |

## 6. 优缺点分析

### Pi

**优点**

- loop 与 session 源码短而直接，适合二次开发、实验新 agent 模式和多供应商模型。
- message tree、branch navigation、完整 JSONL 和可定制 compaction 对调试与可追溯性很好。
- 默认工具面小，prompt/tool schema 成本较低；extension 可在不 fork core 的情况下加入 planner、permission、MCP、subagent。

**缺点**

- 没有内建 sandbox、approval、plan、subagent 和 worktree orchestration；团队若需要企业级安全与并发，必须自己组合并维护。
- extension 拥有完整进程权限，生态包的供应链与 prompt injection 风险由用户承担。
- “无限可扩展”会把架构一致性、权限语义、恢复协议和兼容性责任推给 extension 作者。

### Claude Code

**优点**

- gather/act/verify、Plan、verification guidance、subagent、agent teams、memory、hooks、MCP 与 sandbox 形成完整产品闭环。
- 普通 subagent 用独立 context 隔离噪声，agent teams 又覆盖需要 worker 间通信的复杂场景。
- permissions + OS sandbox + managed settings 比只靠模型服从提示更可靠；local/cloud/remote 覆盖面完整。

**缺点**

- 核心 harness 闭源，外部只能验证产品契约，难以精确审计 planner、context transform、tool scheduler 和恢复边界。
- subagent 与 agent teams 是两套拓扑；后者仍实验性，并有恢复、任务状态和 shutdown 限制，使用成本和 token 成本高。
- Plan 仍由模型生成，不是形式化验证的执行计划；复杂场景依旧需要用户审查范围、依赖和验收标准。

### Codex

**优点**

- core loop、compaction、AGENTS.md、sandbox 与 App Server 均有较多公开源码/协议，可审计性强于闭源 harness。
- App Server 把 thread persistence、工具事件、diff、approval 与客户端 UI 分离，同一 harness 能服务 CLI、IDE、Desktop 和 cloud。
- OS sandbox、approval policy、worktree、cloud container 和 subagent thread 组合成强隔离、强并行的工程环境。

**缺点**

- 架构层次多：model/Responses API、Codex core、App Server、客户端、sandbox、MCP/plugin policy、cloud worker 均有独立语义，集成与运维复杂度高。
- 当前高级 compaction 含 opaque encrypted content，延续性强，但可解释性、跨 provider 可移植性弱。
- 多 agent 提升吞吐同时增加 token、审批路由、线程生命周期和写冲突成本；官方也优先推荐 read-heavy 并行。
- Plan preset 是 prompt/settings 驱动，不应误认为确定性 planner；计划与实际执行仍可能随着新工具观察而改变。

## 7. 对自研 Agent 的可复用结论

1. **不要把 ReAct、Loop、Plan-and-Execute 做成互斥的三个顶层 runtime。** 更稳妥的共同内核是一个可中断的 tool loop；Plan 是一组 mode policy（只读工具、专用 prompt、计划 artifact、审批转换），Execute 是另一组 policy。
2. **任务复杂度路由应先用可观测信号，而不是神秘分类器。** 可用信号包括用户是否明确要求 plan/parallel、预计文件数、是否需要写操作、验收标准是否明确、工具输出规模、依赖能否并行、权限风险和剩余 context。
3. **计划必须是持久化 artifact，不只是 assistant 文本。** 至少保存目标、约束、步骤、状态、证据/验收、阻塞原因，并允许用户修改后再转执行；Codex 的 `plan` item 和 Claude 的 plan approval 都支持这一方向。
4. **把 context state 与 durable state 分开。** conversation/summary 只服务推理；任务状态、权限决定、文件变更、测试证据、subagent edges 应结构化持久化，不能只依赖压缩摘要。
5. **并行默认投向 read-heavy、互不依赖的工作。** 写任务需要 ownership、worktree 或显式文件锁；否则速度收益会被冲突和合并成本抵消。
6. **权限必须在 tool executor/OS 边界强制。** prompt 中写“不要访问”不构成安全边界；Pi 的官方安全说明和 Claude/Codex 的 sandbox 设计都印证这一点。
7. **保留 steering 与恢复语义。** 一个成熟 loop 不只是“直到完成”：还需处理中途用户消息、取消、审批暂停、超时、压缩、resume、fork 和工具失败重试。

## 8. 公开事实与合理推断清单

### 已由一手资料确认

- Pi 默认四工具、无内建 plan/subagent/sandbox，loop、并行工具执行、JSONL tree 和 compaction 均有公开实现。
- Claude Code 官方行为是 gather/action/verify；有 Plan、subagents、实验性 agent teams、permissions/sandbox、auto memory 和 auto compaction。
- Codex 的 model/tool loop、Responses API 输入构成、thread/App Server、Plan preset、subagent threads、sandbox/approval 和 compaction 有官方文档或开源实现。

### 合理推断，不应写成供应商承诺

- 三者的普通输入规划主要是 model-driven dynamic planning，而非每次先运行固定 DAG planner。
- Claude Code 与 Codex 的 Plan 本质更接近“同一 loop 的受限协作模式”，而非完全独立的 planner/executor 服务。
- Pi 的极简架构降低 harness 复杂度，但把一致的安全、并发和企业治理成本转移给集成方。
- Codex 的 opaque compaction 提高同一模型/平台内延续性，但降低跨模型迁移与人工审计能力。

## 9. Lumen 当前实现：运行模式与输入规划链路

> 2026-08-11 更新：本节关于旧 `DelegationManager`、完成门禁和多入口分叉的描述是实施前基线。当前实现已经升级为 Session v8 原生 `AgentOrchestrator`、统一 WorkspaceHost 契约、Agent evidence 与完成门禁；以 [原生多 Agent Runtime 决策记录](../architecture-guide/10-native-multi-agent-runtime.md) 为准。

### 9.1 结论

Lumen 当前不是经典的“双模型 Planner → Executor”，也不是把 ReAct、Loop、Plan-and-Execute 做成三个独立 runtime。它的内核是一个由 Pydantic AI 驱动的结构化 tool loop：模型输出文本或工具调用，运行时执行工具并把结果送回模型，直到得到最终输出。复杂任务的 `set_plan` 是同一模型、同一 loop 中的控制工具；计划被结构化保存和重新注入上下文，但没有独立 planner、DAG scheduler 或 verifier agent。

因此，Lumen 的“模式”应按四个正交维度理解：

| 维度 | 当前选项 | 实际作用 |
|---|---|---|
| 推理/执行 loop | 直接回答；model-driven tool loop；同 loop 内显式计划 | 模型根据上下文决定直接结束、调用任务工具，或先调用 `set_plan` |
| 权限模式 | `manual`、`accept_edits`、`plan`、`auto` | 改变审批与允许的副作用，不会替换核心 loop |
| 工具调度 | `sequential`、`parallel_safe`、`parallel` | 控制同批工具调用的串并行；当前本地配置默认是 `sequential` |
| 交互与委派 | `steer`、`follow_up`；可选深度一只读 delegation | 控制运行中用户输入何时注入，以及是否把有边界的只读子任务交给子 agent |

当前项目配置的有效基线是：权限 `manual`、工具串行、delegation 关闭。TUI 的 Plan mode 还额外注入只读规划提示，并在用户批准后以新的执行 turn 继续；这是一种两阶段 UX，不是另一套 planner runtime。

### 9.2 输入如何变成行动与计划

1. **入口预处理**：适配层展开文件引用、skills 等输入。TUI 还会根据当前模式给 prompt 加说明；Web/headless 目前不会注入等价的 Plan 提示。
2. **上下文装配**：`ContextEngine` 把 system/control instructions、权限与 session policy、当前结构化 plan、项目说明、skills、memory/retrieval、历史消息、当前用户输入和 tool schemas 分区装配，并在超预算时压缩。
3. **模型动态路由**：control instructions 建议在“至少三个动作、任何文件修改、命令执行或多个协调工具”等情形调用 `set_plan`。这是可见的 prompt heuristic，由模型判断，不是确定性 intent classifier。
4. **控制与执行**：`TaskController` 校验并持久化 plan；普通工具先经过 hook、风险分类和审批，再执行。工具结果、拒绝和错误都作为 observation 返回同一个模型。
5. **在线重规划**：模型根据新 observation 继续调用工具、更新步骤、报告进度或提问。计划会作为 task state 在后续请求中重新注入。
6. **结束与持久化**：最终文本、澄清、限制、取消或错误结束 turn；`RunCoordinator` 持久化消息、计划、审批和 checkpoint。

计划结构当前只有 step `id/title/status/note` 与 revision，并约束同一时间最多一个 `in_progress`。它还没有依赖关系、验收标准、证据、owner，也没有 runtime completion guard：模型可以在计划仍未完成时直接给最终答案；`set_plan` 还可以整体替换原计划。因此 Lumen 是“结构化、可观察的动态计划”，但还不是“可形式验证的执行工作流”。

### 9.3 当前实现中的语义分叉与风险

- **TUI 与 Web/headless 路径不一致**：TUI 直接使用 `RunCoordinator`，并实现 Plan prompt 与 plan-review lifecycle；Web/headless 走 `WorkspaceHost`，Plan 主要表现为审批策略阻止写操作。架构文档所称的统一 Host 路径与代码已经发生漂移。
- **Plan 与权限耦合**：`plan` 同时承担“如何协作”和“哪些动作允许”的含义，未来如果要支持“只规划但允许特定探测”“批准计划后自动切执行”等语义，会越来越难扩展。
- **`parallel_safe` 对 MCP 的边界不足**：本地工具会依据风险标注 sequential，但 MCP tool definition 没有同等的强制串行标注；在非 sequential 模式下，带副作用的 MCP 工具可能被同批并发调度。审批仍会生效，但并发安全语义不完整。
- **命令权限不是 OS sandbox**：workspace/path/risk/approval policy 能降低误操作，但无法替代 Seatbelt、bubblewrap、container 等执行边界。
- **计划完成靠提示约束**：没有 verifier 或 completion gate 核对计划状态、测试证据和用户验收条件。

## 10. Lumen 与 Pi、Claude Code、Codex 的实现对比

| 维度 | Lumen | Pi | Claude Code | OpenAI Codex |
|---|---|---|---|---|
| 核心 loop | Pydantic AI 统一 tool loop | 极简公开 TS tool loop | 官方行为为 gather → act → verify | 公开 Rust/Responses tool loop |
| 输入规划 | 模型读取分区上下文；复杂度 heuristic 触发 `set_plan` | 模型 + prompt/extension；无原生 Plan | 模型动态规划；Plan 做只读探索与审批 | 模型动态规划；Plan collaboration preset |
| Planner/Executor 分离 | 无；计划与执行同模型、同 loop | 无内建 | 未公开固定分离；可按阶段切模型 | 无独立 planner service 的公开证据 |
| 计划 artifact | 结构化 plan steps，持久化并回注上下文 | 核心无；extension 自行实现 | 产品内 Plan 可审查，内部结构不公开 | 独立 plan item；与普通 checklist 工具区分 |
| 上下文 | 明确 zone、trust、预算、memory/retrieval、receipts | JSONL 树 + 文本压缩，透明可 fork | tool-output 清理 + summary + memory | thread items + local/remote/opaque compaction |
| 多 provider | 是 | 是，核心优势 | Anthropic 模型为中心，可经云平台部署 | OpenAI Responses 为中心 |
| 多 agent | 可选、深度一、只读、隔离 context | 核心不内置 | subagent + 实验性 agent teams | parent/child agent threads |
| 权限与 sandbox | 风险审批与 workspace policy；无 OS sandbox | 核心无审批、无 sandbox | permissions + OS sandbox | approvals + OS sandbox/cloud container |
| 工具扩展 | built-in/plugin/MCP/skills/hooks | TS extension 可替换几乎一切 | skills/hooks/MCP/plugins | skills/plugins/MCP/App Server |
| 持久化/恢复 | append-only turn、plan、checkpoint、recovery receipt | 完整 JSONL message tree，分支能力强 | transcript/resume/rewind + memory | thread/event lifecycle，客户端协议最完整 |
| 客户端一致性 | Runtime 可复用，但 TUI 与 Host 已有行为分叉 | CLI/print/JSON/RPC/SDK 共享极简 core | 产品统一性强，内部实现不可审计 | App Server 明确隔离 core 与多客户端 |
| 架构复杂度 | 中等；抽象丰富但有重复编排层 | 最低；集成责任最高 | 高；产品闭环完整但闭源 | 最高；隔离与协议能力也最强 |

### Lumen 相对优势

- 比 Pi 更早具备结构化计划、审批、context 分区、持久化恢复、MCP/skills 和有限 delegation，适合作为可治理的平台内核。
- 比 Claude Code 更容易审计和替换关键部件；多 provider、context trust/zone 与 receipts 对私有部署和问题定位有价值。
- 比 Codex 更轻、更 provider-neutral；不用承担完整 Rust core、App Server、cloud runtime 和 vendor-specific compaction 的复杂度。
- 单一 loop 加正交 policy 的方向是对的：不必为 ReAct、Plan-and-Execute、interactive 分别维护三套执行器。

### Lumen 相对短板

- 相比 Claude Code/Codex，缺少 OS sandbox、成熟的 worktree/child-thread lifecycle、可编辑计划审批和强验证闭环。
- 相比 Pi，核心抽象更多，TUI/Host 双编排已经出现语义漂移；message-tree 分叉和“完整原始历史永不丢”的可见性也较弱。
- `set_plan` 比纯文本 todo 更好，但 schema 与状态机仍过轻，计划不具有依赖、验收和完成强制力。
- delegation 只能处理深度一只读任务，安全清晰但无法覆盖实现、测试、评审分别隔离的并行工作流。

## 11. 建议的演进优先级

1. **P0：统一入口语义。** 让 TUI、Web、headless 全部通过同一个 Host/turn preparation pipeline；用契约测试保证相同输入、mode、policy 得到相同 prompt 与 tool boundary。
2. **P0：拆分 CollaborationMode 与 ApprovalMode。** `plan/default` 描述模型如何协作，`manual/accept_edits/auto` 描述副作用如何审批；Plan review 应成为 Host 级状态机，而不是 TUI 特例。
3. **P0：补 OS-level execution sandbox。** 保留现有 risk/approval 作为意图层，再用 sandbox 作为不可绕过的执行边界；MCP 继续要求独立 guardrails。
4. **P0：修正并发安全。** `parallel_safe` 应基于每个工具明确、可验证的 read/write/execute/external effect；未知或有副作用的 MCP 工具默认串行。
5. **P1：增强计划 artifact 与 completion gate。** 增加目标、约束、依赖、验收标准、证据与 blocker；最终结束前检查未完成步骤，并允许模型显式解释为什么取消或重订计划。
6. **P1：增加可控验证回路。** 不必引入永久的第二个 verifier agent；先为高风险修改提供 test/diff/policy evidence gate，必要时再按策略启动独立 review agent。
7. **P1：按实际需求扩展 delegation。** 优先支持独立 worktree/文件 ownership 的实现子任务和可等待、可取消、可恢复的 child run，避免直接开放递归自治。
8. **P2：建立路由评测。** 记录输入特征、是否计划、工具数、重规划次数、审批、失败恢复、测试证据和用户纠偏，用数据校准“何时 direct、何时 plan、何时 delegate”。

推荐的目标形态仍是一个共享、可中断、可持久化的 agent loop；Plan、Execute、Review、Auto 由显式 collaboration policy、approval policy 和 completion gates 组合，而不是演化成多个互相漂移的顶层 runtime。
