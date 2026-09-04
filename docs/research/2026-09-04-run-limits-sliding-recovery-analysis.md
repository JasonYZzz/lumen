# 长任务上限、滑动超时与自动恢复：Lumen / Codex / Claude Code / pi

日期：2026-09-04。状态：实施前研究快照，不是 Accepted 架构决策。
后续升级已落地，当前行为见[实施与验证记录](2026-09-04-run-resilience-upgrade.md)。

范围：用户截图、当前 dirty checkout、当前环境中的依赖源码、公开官方文档，以及已有的 pi
固定版本源码缓存。本文记录研究阶段事实；后续实施的配置与运行行为变更单独记录。

## 结论

截图中的直接触发项是单次模型请求的 **300 秒墙钟截止时间**，不是已经证实的 60 次请求或
100 次工具调用耗尽。Lumen 把请求超时抛成 `LoopBudgetExceeded`，Runtime 再统一渲染成
“Run stopped at a usage limit”，导致故障分类和修复指引错误。

这个截止时间会取消仍在输出 thinking、正文或工具参数的请求。对长代码生成，它可能在
模型即将完成文件内容时丢弃本请求的待执行工具调用，用户再次“继续”又需要重新生成。
截图不能证明本次请求究竟是在持续生成、网络静默，还是重复思考；需要逐请求活动计时才能区分。

建议方向：**默认允许正常长任务持续推进，用滑动空闲超时处理连接停滞，用有界重试处理
瞬时失败，用 ContextEngine 管理上下文，用可验证的轨迹诊断处理无效循环；用户显式设置的
预算仍然是硬限制。** 不建议自动把显式预算不断加大，也不建议只把 300 改成一个更大的固定值。

## 1. 当前代码的事实与原始设计依据

| 事项 | 当前事实 | 证据 |
| --- | --- | --- |
| 请求数量 | 默认 50，本项目配置 60；每次 `run()` 从零计数，在下一次逻辑请求前检查 | `config.py:74`；`agent_loop/loop.py:1029,1047` |
| 工具数量 | 默认及项目配置均为 100；执行整批调用前检查能否容纳该批次 | `agent_loop/loop.py:1275` |
| 模型请求期限 | 默认 300 秒，包含当前逻辑请求的流与 Loop 内 Provider 重试；不覆盖工具执行和审批等待 | `config.py:95`；`agent_loop/loop.py:1066` |
| 超时分类 | 自己的 deadline 到期后抛 `LoopBudgetExceeded`，随后附上 request/tool caps 的通用提示 | `agent_loop/loop.py:1075`；`runtime.py:529,1978` |
| 安全重试 | 最多额外尝试 2 次；仅限 retryable 错误且尚无文本、thinking、工具调用活动；Loop 本身直接重试，没有退避等待 | `runtime.py:485,1809`；`agent_loop/loop.py:796,966` |
| 进行中的响应 | 只有完整响应通过协议检查后才执行已收集的工具调用 | `agent_loop/loop.py:1066,1260` |
| 重复轨迹 | 保存最近 8 个工具结果签名，从第二次重复开始产生诊断；该路径不主动改变策略或停机 | `agent_loop/loop.py:702`；`runtime.py:1767` |
| 上下文 | run 开始可做摘要；每个请求冻结前还有工具正文收据化、旧历史裁剪和容量检查 | `runtime.py:988,1211`；`context/engine.py:1583` |
| 自动扩大输出预算 | length stop 后可扩大隐式输出额度，最多重试 3 次；显式 `max_tokens` 不扩大 | `runtime.py:1510`；`config.py:85` |

这里的 `request_count` 更准确地说是逻辑模型步骤数量。`_stream_request()` 内的 Provider 尝试
没有各自增加 `_request_index`，所以不能把界面上的 requests 当成精确 HTTP 调用数或付费次数。

默认 50/100 的代码注释给出了设计动机：按八步计划估算模型请求数，留反思与重试余量，同时
避免失控消耗。这是粗略工作量启发式，没有基于任务进度或模型速度自动伸缩。

300 秒则来自此前的
[Skill 安装故障修复](2026-09-04-skill-install-failure-audit.md)：模型曾用文本生成搬运大量
base64，计数上限不能打断单次持续输出，因此增加请求总时限作为止损措施。动机合理，但把
特定失控场景的止损手段推广成所有模型请求的默认硬截止，会误伤正常长推理与代码输出。
错误消息还无条件建议“direct file transfer”，即使当前任务是在创作代码，也会出现下载提示。

“no collected tool calls were executed”只说明**被取消的这次请求**的待执行调用没有交付。
它不表示整个 run 没有修改文件，更不表示前面执行过的副作用已经回滚。

## 2. 参照产品实际做了什么

### Codex：300 秒是空闲超时

官方配置文档明确区分：SSE 空闲超时默认 300000 ms，HTTP 请求重试默认 4 次，流中断重试
默认 5 次；另有自动压缩阈值。不能把这些重试数字简单相乘为一个对所有故障都成立的保证，
也不能据此声称所有桌面端、所有 Provider 都有同样的续接能力。

来源：[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

对本次问题最有价值的是计时语义：

```text
Lumen 当前：截止时刻 = 请求开始 + 300 秒
建议的滑动空闲：截止时刻 = 最近一次流活动 + 300 秒
```

例如模型每 10 秒产生数据，持续 12 分钟：它超过前者，但一直没有触发后者。
这不等于取消所有上限，也不保证断开的请求可以从同一个 token 无缝恢复。

### Claude Code：计数限制是显式选项，上下文自动管理

官方 CLI 文档将 `--max-turns` 标为 print 模式选项，默认没有该次数限制；还提供显式
`--max-budget-usd`。官方运行说明描述自动清理旧工具输出、需要时总结对话，并在压缩反复
失效时停止，避免压缩循环。

来源：[CLI 参考](https://code.claude.com/docs/en/cli-reference)、
[运行原理](https://code.claude.com/docs/en/how-claude-code-works)。

因此，不能把 Lumen 的默认固定 50/100 次上限当成编程 Agent 必需的设计。另一方面，
Claude Code 的公开文档不足以证明其内部精确流超时和所有重试条件；不推测私有实现。

### pi：空闲超时、有界重试、同一 run 内压缩后继续

当前官方设置文档列出 `httpIdleTimeoutMs=300000`；自动重试默认开启、最多 3 次，
基础延迟 2000 ms，指数退避。Provider 层重试默认 0，并对服务端要求的重试等待设置上限。
这将网络恢复集中在 Agent 层，也避免长时间静默等待。

来源：[pi 设置](https://pi.dev/docs/latest/settings)。

压缩文档明确说明：工具结果追加后、下一次模型响应前检查容量，在同一 run 内摘要并继续。
默认预留输出 16384 tokens，保留近期 20000 tokens；这些是 pi 的设置，不是 Lumen 应照抄的
通用模型参数。来源：[pi 压缩机制](https://pi.dev/docs/latest/compaction)。

另核查了已有固定版本 `3316c4e35b1eb505e791610d3a97d6b4c1c48309`：

- `agent-loop.ts` 的循环由工具调用、steering、follow-up 和终态驱动，所检查的核心循环没有
  Lumen 式 50/100 次硬墙。
- `agent-session.ts` 在成功响应后重置连续失败计数；重试前保留 Session 中的错误记录，
  从模型活动上下文移除失败响应，再进行可取消的退避等待。

源码：[Agent loop](https://github.com/badlogic/pi-mono/blob/3316c4e35b1eb505e791610d3a97d6b4c1c48309/packages/agent/src/agent-loop.ts)、
[AgentSession](https://github.com/badlogic/pi-mono/blob/3316c4e35b1eb505e791610d3a97d6b4c1c48309/packages/coding-agent/src/core/agent-session.ts)。
仓库现在跳转到 `earendil-works/pi`，这里保留固定版本来源；当前文档与固定源码分开引用。

三者均不能凭用户“从未遇到”推导出没有额度、模型输出限制、网络失败或安全门禁。
更有解释力的差异是：通常把可恢复的基础设施问题自动处理，不把它们直接暴露成普通任务失败。

## 3. “自动滑动”应分四种情况

| 对象 | 是否自动滑动 | 推荐处理 |
| --- | --- | --- |
| 流空闲时限 | 是 | 接收有效活动后更新 idle deadline |
| 上下文 | 是 | 请求之间做摘要/裁剪，完整历史留在 journal |
| 连续故障计数 | 成功后重置 | 只限制连续失败，累计尝试和消耗继续记账 |
| 用户预算 | 否 | 不因输出、压缩、续跑或换 segment 而自动扩大 |

以“每用完 60 次自动再加 60 次”实现滑动，会让预算失去含义。以“每产生一些文字就认定
任务有进展”续费，也会奖励重复思考。建议新配置允许不用计数硬墙，并另设可解释的无进展诊断。

## 4. 推荐的目标行为

### 4.1 分开空闲超时和可选硬期限

- 增加 `model_stream_idle_timeout_seconds`，建议默认 300 秒。
- 保留 `model_request_timeout_seconds` 的旧含义作为可选总时限；新默认可为 `null`。
  旧配置明确写了 300 时仍表示总时限 300，不能静默改成 idle。
- 连接建立使用独立、较短的网络超时；首个事件迟迟未到也必须可超时。
- 持续 thinking、正文和工具参数属于流活动，应该维持连接等待；它们不自动证明任务取得进展。
- Provider 原始 SSE 心跳常被 SDK 消费，不一定出现在当前 ModelDriver 的标准事件中。
  传输存活检测应放在 Driver/HTTP 所能观察的地方；不能简单对 UI 文本做刷新就声称等价于
  网络 idle。若只能实现标准事件空闲计时，要明确其语义和隐藏推理的兼容限制。
- sink/UI 的背压、审批等待、工具执行不应被统计成模型空闲；计时与事件渲染分离。
- 只有心跳没有内容时可提示等待状态。不要仅因为“思考很久”就由程序宣判循环；需要更严格
  止损的无人值守调用者可以显式启用硬期限。

以上默认值是提案，需要用不同 Provider 的首事件延迟、最大事件间隔分布校准，不能从截图
计算出最合适的期限。单纯按预期 token 数推算 deadline 也不可靠：推理 token、吞吐和代理
缓冲行为都会变化。

### 4.2 有界、可取消的自动恢复

沿用 `LumenAgentLoop` 作为重试权威，按故障和执行事实处理：

| 状态 | 自动处理 |
| --- | --- |
| 连接错误、短暂 429/5xx，且请求还没产生可用响应 | 指数退避加抖动，尊重合理的 Retry-After，有限重试 |
| 已产生部分 thinking/正文/工具参数，但本次尚未执行工具 | 在 Driver 声明不存在未知远端副作用的前提下，可丢弃未完成候选，重新请求 |
| 前面模型步骤已经执行工具 | 保留已完成步骤及 receipt，从最后完整步骤恢复；不重跑整个 run |
| Provider 内置工具、外部动作或执行结果不明 | 使用既有 reconciliation 路径，不自动重放未知副作用 |
| 输出长度耗尽 | 复用现有输出续接策略，不执行残缺 JSON，不自动扩大显式 max_tokens |
| 上下文溢出 | 交给 ContextEngine 有界压缩后再请求，不原样无限重试 |
| 用户取消、凭据错误、实际额度耗尽、显式硬预算耗尽 | 停止或明确暂停，不能伪装成短暂网络错误持续重试 |

初始建议最多 3 次恢复、退避 2/4/8 秒加抖动，恢复成功后重置连续失败计数。
实际模型尝试、时间、已知 usage 累积保留，收到部分文本不算恢复成功。很长的 Retry-After
应转成明确等待/暂停状态，不能让 UI 无限显示“正在处理”。

部分候选仍作为失败证据保留，并在 UI 标记已中断/已替换；不将半截正文无标记拼接到新响应，
不伪造 assistant 成功消息，也不把残缺工具参数塞回有效工具轨迹。

当前依赖另有一个必须先处理的事实：`build_model()` 使用默认 SDK client，安装的
OpenAI/Anthropic SDK 各默认重试 2 次；PydanticAI 的 HTTP client 默认 600 秒、连接 5 秒。
Loop 又最多尝试 3 次。因此某些失败路径可能产生多层尝试，而 UI requests 仍只算一个逻辑
步骤。实施时应让 SDK 重试服从 Loop 的预算，优先关闭重复重试，并为每次实际尝试保存
attempt 身份和时间。不能承诺所有 Provider 都恰好产生 9 次 HTTP 请求。

### 4.3 计数预算可选，显式配置保持有效

建议 `request_count` 和 `tool_calls` 支持 `null`，新交互项目默认不设这两项硬上限。
同一份有效 LimitsConfig 经 Host/Runtime 进入所有客户端，不能 Web 无限而 headless 仍偷偷
使用另一套默认值。无人值守任务和 child Agent 可以继续配置明确的预算；子 Agent 的有限
预算、权限收窄和父级预算约束不能随根 Agent 的默认调整而解除。

旧配置里的 60/100 继续生效，并清楚显示配置来源。达到明确预算时提供结构化的恢复入口，
例如增加本任务剩余额度，写入审计事实后继续。不能仅发送一条“继续”就偷偷绕过用户选定的
硬预算，更不能给 UI 单独增加预算状态权威。

长期计费控制应使用能够可靠计量的成本/用量预算；缺少价格或 usage 时显示未知/估计，不能
声称精确美元上限。此项不应成为修复 idle 和重试的前置条件。

建议目标配置示意，**当前 schema 不支持，不能直接复制使用**：

```yaml
agent:
  limits:
    request_count: null
    tool_calls: null
    model_stream_idle_timeout_seconds: 300
    model_request_timeout_seconds: null
    tool_timeout_seconds: 60
```

`tool_calls: 0` 仍然表示禁止工具调用，不要把 0 重定义成无限。

### 4.4 扩展已有上下文滑动，而不是创建第二个 ContextManager

Lumen 已有自动摘要，不能说它“完全没有自动滑动”。但 `prepare()` 位于 run 开始处；
`adapt_request_history()` 是同步方法，在单个长 run 内触及硬限时收据化工具正文、裁掉旧的
canonical user turns，保护活动轨迹的配对顺序。活动轨迹本身过大时仍可能失败。

建议在完整工具批次之后、下一模型请求冻结之前，增加 ContextEngine 的异步 prepare 能力，
对已完成的活动前缀做增量摘要。保留最近工具调用/结果、当前目标、审批、未验证 effect、
Agent 完成责任和准确路径；摘要失败沿用现有降级与防抖逻辑。

checkpoint 必须在对应 journal 事实持久化后发布；压缩不重置调用预算、不新造用户 turn，
不改变 canonical history，也不能重复注入 Skill/Memory/MCP 内容。

### 4.5 从重复诊断发展到可解释的无进展处理

复用已有最近 8 次工具轨迹签名。先观察和提示，再做有限的策略修正；只有持续重复失败、
同样动作/结果且没有新的工作证据，才暂停并报告具体原因。

可参考新文件 revision、验证结果变化、新获取且相关的资料、已完成子 Agent 结果。单纯
“工具成功”、更新时间或模型自报完成不能独立证明进展；正常等待后台任务必须由既有
Orchestrator/polling 状态区分，不能按重复轮询直接判死循环。

不建议首版就为所有研究/创作任务自动评估“进度百分比”或自动加预算。这比修复空闲超时
复杂得多，而且误判率高。先记录诊断并用真实轨迹校准阈值。

## 5. 实施位置和顺序

1. **P0：修正分类和滑动计时。** 在 Loop 中使用独立的 timeout 原因，不再抛预算错误；
   Runtime 输出对应的结构化故障、实际计数、elapsed/idle、可重试性。新增 idle 配置与可选
   hard deadline，保留显式旧值语义。删除无条件“下载/调 request_count”的错误提示分支，
   因为 timeout 已由同一 Loop 的准确原因替代。时间记录不包含正文和凭据。
2. **P1：安全自动恢复。** Loop 拥有 attempt、退避、取消和重试状态；Driver 翻译传输错误并
   配合关闭重复 SDK 重试。Runtime 投影恢复进度；中途恢复不先发终态 RunFailed。最终只产生
   一个 completed/failed/cancelled 等终态，Host 和 Coordinator 不另起“自动继续”循环。
3. **P1：可选计数限制。** 配置支持 null；旧有限值保留；给显式预算暂停提供共享 Host 命令。
   校验 child 收窄和预算继承，明确 logical request 与 attempt 的统计口径，不静默改旧字段含义。
4. **P2：请求间增量压缩和进度诊断。** 扩展 ContextEngine；复用 TaskWorkspace/Orchestrator
   证据，补齐同一长 run 的 checkpoint 持久化后再允许其成为恢复权威。

不需要新增通用的 RetryManager、BudgetManager、ProgressService 或另一套调度 Module。
新的 Interface 应把复杂实现留在现有 Loop、ContextEngine、Host 的合适 Locality。

配置若以 v2 的可选字段扩展实现，应检查 strict schema、旧配置与生成契约；若需要新增
Session record 或更改已有 record 语义，应正式设计 schema 迁移，不能绕过 v9 兼容要求。
旧会话加载只读，历史失败记录不回写成成功，配置文件也不自动重写。

## 6. 验收标准

| 场景 | 目标结果 |
| --- | --- |
| 总生成时间超过 300 秒，但持续有非空文本、thinking 或参数事件 | 默认不中断；显式 hard deadline 仍按时生效 |
| 无首事件、流中途静默、只有网络心跳、SDK 隐藏 thinking | 各自按声明的 idle 语义工作，原因可区分，不误算 UI 背压 |
| 部分文件参数生成后断流 | 未执行残缺调用；有限恢复后完整调用最多执行一次 |
| 前一个步骤已写文件，后一个模型请求超时 | 恢复保留已写文件和验证收据，不重复执行原写入 |
| 可能已经发生远端副作用 | 转 reconciliation，不自动重放 |
| 退避期间用户停止 | 立即取消，不再发新请求，只有一个终态 |
| 429 短等待、过长 Retry-After、401、额度用尽 | 分别恢复、明确暂停、停止；不统一无限重试 |
| 达到用户显式 60/100 次限制 | 准确报告触发项，预算未获调整时不自动扩额 |
| null 计数预算下正常完成第 61 个请求/第 101 个工具调用 | 正常完成；tool_calls=0 和有限 child 预算仍有效 |
| 单一用户 turn 中完成许多工具批次后上下文接近上限 | 请求间压缩后继续，canonical journal 不丢失、不改写 |
| 连续相同失败、合法后台轮询、不断变化的有效研究结果 | 能区分；不会把有输出或一次成功直接当成任务完成 |
| Default / Plan、TUI / Web / headless、恢复旧 Session | 同一后端原因、同一策略，审批和完成门禁保持有效 |

测试应使用可控时钟/Driver 将分钟压缩到毫秒，覆盖预期状态与副作用，而不真的等待五分钟。
随后按仓库要求执行相关 pytest、Ruff、Pyright；Runtime/config/context/Session 变更跑全量
pytest，Host/API 变更检查契约与 OpenAPI，Web 变更跑测试、typecheck、build。

## 7. 本次实际验证与立即缓解

已执行：

```bash
uv run pytest -q tests/test_lumen_agent_loop.py tests/test_runtime.py \
  -k 'deadline or midstream_provider_failure or usage_only_activity or transient_provider'
```

选中 6 个测试，全部通过。它们确认当前实现确实会取消持续 thinking 的流、不交付待执行
工具，并保留 Runtime 的部分文本和已收到 usage；也验证当前中途失败不自动重试的行为。
这验证的是故障分析中的**现状**，不是上述提案已实现或修复完成。

本次仅新增研究文档并更新索引，没有删除生产代码或测试，没有改配置、提交、推送或发布。
没有运行全量测试、静态检查和构建，因为没有更改实现。

如果必须在架构修复前缓解当前中断，可临时把现有 `agent.limits.model_request_timeout_seconds`
提高到例如 1800 秒；这是经验性缓解，不是经测量确定的最优值。应让新 run 使用重载后的配置，
不假定正在运行的进程会热更新。调高 60/100 不解决这次 300 秒超时。即使改成 1800，仍有硬
截止和缺少中途自动恢复的问题；它不应成为最终交付。
