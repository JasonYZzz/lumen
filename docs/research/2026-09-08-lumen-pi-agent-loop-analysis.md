# Lumen 与 pi AgentLoop 源码对照及长耗时分析

> 后续状态：本文的工具并发/结果发布对照记录的是优化前行为。随后已将 Lumen 默认改为 `parallel_safe`，并在工具完成时发布结果。推理档位和原生子 Agent 并发的进一步源码复核见 [推理强度与子 Agent 并发](2026-09-08-reasoning-and-agent-parallelism.md)；“pi 默认并行”不能等同于默认自动拆分子 Agent。

2026-09-08。研究对象为本地源码：Lumen HEAD `250adf89903761260413577848519e818b825f16` **加当前未提交修改**；pi HEAD `b2602be77cb7b0de45dd616407fd210daa48aa75`，检查时工作区干净。本文是研究结论，不是新的 Accepted 架构规范。未修改生产代码、配置、原 Session 或 pi；没有重新运行付费模型任务。

**结论：Lumen 的主循环结构没有发现需要推倒重写的错误，但模型可见的任务控制 Interface 确实存在额外往返成本。已有长任务的主要证据指向“很长的单次模型推理，与过多计划/进度控制往返叠加”。工具串行、输入规模和展示延迟是次级因素。不能据此判定所有模型、所有任务都比 pi 慢。**

本次分别使用源码、原始 journal 的聚合统计和不联网的受控模型验证。此前的研究文档仅用于定位案例；下述主要数字重新从原始 journal 计算，没有引用或展示私有思考正文。

**1. 两边实际运行的是怎样的循环**

```text
pi coding-agent
  AgentSession → Agent → runLoop
    准备上下文/模型设置 → 一次流式模型请求
    ├─ 完整工具批次 → 执行 → 回传结果 → 下一次请求
    ├─ 有 steering/follow-up → 加入消息 → 下一次请求
    └─ 无工具、无待处理消息 → 结束
  AgentSession 在外围处理自动重试、压缩和会话持久化

Lumen
  WorkspaceHost → RunCoordinator → AgentRuntime → LumenAgentLoop
    ContextEngine 预检/按需压缩 → 冻结请求 → 一次流式模型请求
    ├─ 完整工具批次 → CapabilityGateway 验证/审批/执行 → 回传 → 下一次请求
    ├─ 有交互输入 → 加入消息 → 下一次请求
    ├─ 可恢复传输失败/截断/溢出 → 对应恢复路径
    └─ 无工具 → CompletionGate 本地检查 → 通过则结束
                                  └─ 可由模型修复的阻塞 → 反馈后继续
```

两边都在一次模型响应内接收 thinking、text、tool call。**没有发现 Lumen 默认额外调用一个“思考模型”再调用“执行模型”；也没有发现原生 Loop 外面还套着 PydanticAI Agent graph。** Driver 使用低层 `Model.request_stream()`。状态枚举、状态迁移和 Gate 检查本身都不等于额外模型请求。Context 摘要等辅助模型调用需要单独计量，但不是每个工具前的强制思考阶段。

源码：[pi runLoop](/Users/admin/IdeaProjects/pi/packages/agent/src/agent-loop.ts:167)、[pi 请求转换](/Users/admin/IdeaProjects/pi/packages/agent/src/agent-loop.ts:296)、[Lumen Loop](/Users/admin/IdeaProjects/lumen/src/lumen/agent_loop/loop.py:1202)、[低层 Driver](/Users/admin/IdeaProjects/lumen/src/lumen/agent_loop/pydantic_driver.py:289)、[Runtime 接线](/Users/admin/IdeaProjects/lumen/src/lumen/runtime.py:1905)。

| 维度 | 当前 pi 本地源码 | 当前 Lumen 本地源码 | 对耗时的意义 |
| --- | --- | --- | --- |
| 主模型请求 | 工具结果之后继续；无工具通常结束 | 相同基本模式，增加完成检查与恢复 | 不是所有额外状态都增加推理轮数 |
| 推理设置 | Agent core 初始 off；coding-agent 应用默认 medium，并恢复 Session/用户/模型设置 | `settings` 默认空，保留 Provider 默认；显式配置才传递相应选择 | 必须对齐最终请求参数，不能把两种默认视为一致 |
| 计划与进度 | 默认提示和四个编码工具没有 Lumen 式计划/验收记账 | 多阶段工作要求 set_plan、及时 update_step；可选 link_evidence、report_progress | 模型逐个调用会制造纯控制请求 |
| 工具并发 | Agent 默认 parallel；批次里存在 sequential 工具时整个批次串行 | 默认 sequential；启用后只并发相邻 PARALLEL_SAFE 调用，exclusive 是屏障 | 同一批次执行时间不同；不会自动减少模型请求数 |
| 完成条件 | 无工具和队列消息通常结束；支持 stop/terminate 扩展 | 本地检查 Plan、Work Product、Agent 等；可恢复问题默认最多反馈两次 | 可产生额外修复请求，但并非每次都发生 |
| 重试 | core 遇错误返回；AgentSession 默认最多 3 次指数退避；Provider 另有可配请求重试 | 主模型 Loop 默认最多 5 次安全重试；对应 OpenAI/Anthropic SDK 重试关闭 | 异常路径可能变慢，不能用正常任务总轮数推断重试 |
| 上下文 | transformContext、convertToLlm；AgentSession 下一响应前可压缩 | 每步做预算、适配、digest/manifest；达到条件才压缩 | Lumen 本地准备工作更多，但尚无 CPU profile 证明是分钟级主因 |
| 工具流展示 | 参数 delta 可见；执行完成事件按完成顺序，持久结果按原顺序 | 新修改显示“生成参数中”；Loop 主要结果事件在批次全部完成后发布 | 有真实的“已执行但界面还没显示完成”的可能 |
| 缓存 | Anthropic Adapter 默认 short，按兼容能力设置 cache_control | PydanticAI 缓存字段需配置；第三方 Provider 也可能自行缓存 | 有默认差异，但本案例实际已报告大量缓存命中 |

这里的 pi 指默认 coding-agent 和其底层库；用户扩展也可以给 pi 增加复杂计划、审批、工具和完成策略，不能把默认差异理解成能力上限。

**2. 长任务的原始证据：控制往返是真实发生过的**

复核已有 HTML 报告任务 journal，Session `a6b48e3d-0556-45f0-b1eb-10d121dbae4b`。这是此前版本执行留下的历史数据，不能假定它已运行当前未提交修复。

| 指标 | 重算结果 | 解释 |
| --- | --- | --- |
| 逻辑模型请求 / 模型尝试 | 23 / 23 | 没有主模型传输重试的计数证据 |
| 已完成 assistant 工具响应批次 | 22 | 第 23 次请求未留下成功响应 |
| 工具调用 | 31 | 历史 usage.tool_calls=0 是旧计数缺陷，timeline/消息中有实际调用 |
| 计划/进度控制调用 | 16 / 31 | set_plan 4、update_step 8、link_evidence 2、report_progress 2 |
| 纯控制工具批次 | 11 / 22 | 半数已完成批次没有调用检索、读写、命令等工作工具 |
| 工具错误 | 7 | set_plan 2、update_step 2、link_evidence 2、report_progress 1；全是控制调用 |
| 原生 thinking 文本长度 | 176,424 字符 | 仅统计长度，不是 token 数，也不是最终报告长度 |
| 输出 usage | 67,025 tokens | 模型输出总量，不能直接等同 thinking tokens |
| 工具 elapsed_seconds 之和 | 61.97 秒 | 包含批次等待/审批等，不能当成独占工具耗时 |
| 输入 usage / 缓存读取 usage | 2,030,976 / 1,829,248 | 累计 usage，不是单次上下文长度；不能断言完全没有缓存 |

最明显的两个轨迹片段：

```text
第 9 个完成批次：29,782 字符 thinking → 2 次 update_step
第 22 个完成批次：78,281 字符 thinking → 3 次 update_step
```

第 22 次请求开始于 run 的约 1534.38 秒，第 23 次开始于约 2383.72 秒，间隔约 **849 秒（14.2 分钟）**。这个间隔包括模型流、工具批次及下一请求准备，不能全部标记为模型计算时间。不过，结合极长 thinking 与仅三个状态更新，足以支持：**模型在一次控制批次前消耗了非常长的生成过程，而工作执行没有随之推进到最终 HTML 交付。**

需要保留两条限制：纯控制批次前的思考可能包含实际研究分析，不能把全部思考标成浪费；同样，移除这些控制工具后，不能保证这些思考就自动消失，模型可能把它们移到其他请求。

案例失败前没有正常终答候选的轨迹，也没有 completion retry/compaction 事件证据。**不能把这次约 56 分钟的长任务归因于 CompletionGate 不断拒绝最终答案、自动重连风暴或反复压缩。**最后一次失败的旧错误信息为空，不能进一步断言其原因是超时、Provider 故障或配额。

**3. 最重要的设计问题：把状态维护变成模型必须反复完成的工作**

Lumen 的 `CONTROL_INSTRUCTIONS` 要求多阶段任务建立计划、步骤开始和结束立即更新、需要验收时关联 evidence。它经 ResourceManager 加入生产指令。`update_step` 和 `link_evidence` 通过与普通工作工具相同的 Loop 回传路径运行。

如果模型选择分开调用，就会形成：

```text
模型 → set_plan → 模型 → update_step(in_progress)
     → 模型 → 实际工作工具 → 模型 → link_evidence
     → 模型 → update_step(completed) → 模型 → 最终答案
```

这些工具的 Python 执行可能很快，但每个独立批次都有一次新的 Provider 往返、输入处理和可能的长推理。**问题主要在模型可见 Interface 的使用成本，而非 TaskController 方法执行慢。**

此前版本还存在证据 Interface 不闭合：执行器生成 receipt ID，但模型观察不到可用 ID；模型却必须向 `link_evidence` 提供该 ID。案例里两次猜错 evidence ID、两次无法完成验收、随后重建计划，与这一缺陷相符。当前 dirty 修改已经在工具回传中加入 `plan_evidence`，补充错误提示和恢复上下文，同时把单次编辑/命令的强制规划改成多阶段规划。**这是已有修复，本次没有重复实现；它解决可用性问题，但不会自动消除所有纯控制往返。**

源码：[任务控制提示](/Users/admin/IdeaProjects/lumen/src/lumen/runtime.py:471)、[生产指令装配](/Users/admin/IdeaProjects/lumen/src/lumen/resources.py:1027)、[步骤与证据验证](/Users/admin/IdeaProjects/lumen/src/lumen/task_control.py:125)、[当前 evidence 回传](/Users/admin/IdeaProjects/lumen/src/lumen/runtime.py:1454)、[pi 默认系统提示](/Users/admin/IdeaProjects/pi/packages/coding-agent/src/core/system-prompt.ts:29)。

**4. 不联网的受控验证：成本可以降低，且不必取消验证**

使用当前 `AgentRuntime`、真实 `TaskController`、真实 Gateway 和 `FunctionModel` 模拟固定模型输出。三个场景都只调用一次 `inspect_report`；两个计划场景均保留相同的验收条件、真实生成的 receipt ID、依赖关系和完成检查。

| 合成场景 | 模型请求数 | 全部工具调用 | 实际检查工具调用 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 普通无计划流程 | 2 | 1 | 1 | completed |
| 两步骤计划，控制逐条发送 | 8 | 7 | 1 | completed |
| 相同计划与验收，已知参数合批 | 3 | 7 | 1 | completed |

合批场景的具体顺序：

```text
请求 1：set_plan → update_step(开始验证) → inspect_report
请求 2：link_evidence → update_step(验证完成)
        → update_step(开始交付) → update_step(交付完成)
请求 3：最终回答
```

执行器仍使用默认串行，因此不会并发修改计划。第一批返回真实 receipt ID 后，第二批才引用它，没有猜测未知结果。所有场景真实工具只执行一次，均无工具错误。

这证明当前实现已经支持“一个模型响应里的多个有序调用”，不必先增加新的批处理 Module。它是请求数的机械验证，**不是 pi/Lumen 的真实模型速度 A/B，也不保证模型在自然提示下会采用同样批次**。最先值得实验的是更清晰的合批指引和更少的非必要计划控制，而不是新建另一套状态权威。

**5. 思考长度由什么决定，为什么不能只调 max_tokens**

pi 有两个容易混淆的默认值：底层 `Agent` 的初始 `thinkingLevel` 是 off，实际 coding-agent 的 `DEFAULT_THINKING_LEVEL` 是 medium；SDK 再结合恢复会话、模型、用户设置选择并 clamp。Lumen 模型 settings 默认为空，不强行改变 Provider 的原默认。

本地项目配置只给主要模型设置了较大的 `max_tokens`（DeepSeek 路由 65,536，Qwen Anthropic 路由 131,072），没有显式 thinking/effort。**由此能确定的是请求未明确选择更低推理强度，不能仅从 YAML 推断 Provider 的实际推理预算。** max_tokens 是上限/输出预留相关参数，不代表模型必须输出这么多，也不应作为所有协议通用的思考开关。

pi 的预算型 Anthropic 映射为 minimal=1024、low=2048、medium=8192、high=16384，并结合回答空间调整；自适应模型走 effort，特殊兼容模型另有分支。Lumen 由安装的 PydanticAI Provider Adapter 映射。因此，即使 UI 名称相同，也必须检查路由、模型 profile 和最终 HTTP body 才能认定等价。

当前 dirty Driver 已修复：显式 `thinking=False` 在 Anthropic 兼容路由应发送 disabled；未知 Responses profile 的显式设置不能静默丢失；原生 Provider 设置优先。它仍刻意保留“未设置”的语义，不能把这些修复理解成已经默认关闭了推理。

隐藏或折叠 thinking UI 不会减少模型实际生成；完整思考、思考摘要和最终答案长度也不是同一个指标。本文不建议为了“看起来快”丢弃原生 reasoning item、签名或恢复所需信息。

源码：[pi 应用默认](/Users/admin/IdeaProjects/pi/packages/coding-agent/src/core/defaults.ts:3)、[pi SDK 选择](/Users/admin/IdeaProjects/pi/packages/coding-agent/src/core/sdk.ts:229)、[pi Agent 默认](/Users/admin/IdeaProjects/pi/packages/agent/src/agent.ts:77)、[预算映射](/Users/admin/IdeaProjects/pi/packages/ai/src/api/simple-options.ts:58)、[Anthropic 路由映射](/Users/admin/IdeaProjects/pi/packages/ai/src/api/anthropic-messages.ts:860)、[Lumen settings 默认](/Users/admin/IdeaProjects/lumen/src/lumen/config.py:59)、[当前 Driver 映射](/Users/admin/IdeaProjects/lumen/src/lumen/agent_loop/pydantic_driver.py:298)。

**6. 次要差异与容易误判的点**

- **并发默认确实保守。** Lumen `LimitsConfig.parallel_tool_calls` 默认 sequential；本地项目 YAML 没有覆盖，示例 YAML 的 parallel_safe 不会自动应用到现有配置。pi Agent 默认 parallel。但 Lumen 启用 parallel_safe 只会合并明确安全的相邻调用，MCP/未知副作用不会自动获得并发资格。它适合缩短独立检索等工具等待，不能解决一次 14 分钟的模型生成。模型“一次返回多工具”与“工具同时执行”是两个独立维度。
- **工具时间的统计有系统性混杂。** Lumen 在执行批次前为全部调用设置 started_at，Loop 等 `_execute_tools` 返回整批结果后再发 `LoopToolResultRecorded`。即使第一个工具已经结束，它也可能等到最后一个完成才有主要完成事件。因此 elapsed 含其他调用等待，跨工具求和会重复计量。pi 并发路径在单个工具完成时发结束事件，最终按源顺序组装消息。这是观测和体验方面明确可改的 Implementation，不能直接把目前时间字段当性能 profile。
- **输入不小，但不能全算推理负担。** 案例每请求 instructions 估算 2354 tokens，tools 6620–10568，messages 667–126235。模型 receipt 的 total_tokens 还包括 131072 的输出预留，不能将它全称为“输入 prompt”。稳定指令和工具可缓存，变化历史仍可能增加处理成本。没有同模型、同任务 pi 请求体，不能给出“prompt 大 N 倍”的结论。
- **没有逐 token fsync 的证据。** Coordinator 累积 timeline，Host EventJournal 主要在内存追加；turn journal 在运行结算时持久化。每步序列化、token 估算、manifest digest 等仍可能成为大上下文热点，但需要 CPU/阶段计时，不支持直接归因于 Python 或 Pydantic。
- **完成门禁是必要差异。** 当前 Gate 默认本地检查；无问题时立即接受，可由模型修复的问题最多反馈两次，非模型可恢复阻塞直接交还恢复路径。两次反馈不是“修复期间只允许两个模型请求”，每次反馈后仍可有若干工具轮。保留 Work Product 验证、批准计划和 Agent 结果收敛，不应通过取消门禁伪造成功。
- **停滞检测目前只记录事实。** 相同工具名、参数、状态和结果签名重复时产生 `LoopStallObserved`；它不自动终止或纠偏。未设预算时，模型重复有效但无进展的调用可能持续。但本案例最大思考批次不需要重复签名，因此这个检测也无法直接捕获它。
- **缓存不是本案例已证实的主因。** pi Anthropic 默认设置短缓存；Lumen 没有自动启用全部 Anthropic 缓存设置。但案例报告 1,829,248 cache-read tokens，第三方 Provider 的缓存机制已发挥作用。不能因没有显式 cache_control 就声称每轮全部重新计算。

源码：[Lumen 并发分批](/Users/admin/IdeaProjects/lumen/src/lumen/agent_loop/loop.py:601)、[pi 并发选择](/Users/admin/IdeaProjects/pi/packages/agent/src/agent-loop.ts:418)、[Lumen 批次结果发布](/Users/admin/IdeaProjects/lumen/src/lumen/agent_loop/loop.py:625)、[Runtime 计时](/Users/admin/IdeaProjects/lumen/src/lumen/runtime.py:1755)、[上下文每步准备](/Users/admin/IdeaProjects/lumen/src/lumen/context/engine.py:800)、[事件 journal](/Users/admin/IdeaProjects/lumen/src/lumen/application/events.py:125)、[完成策略](/Users/admin/IdeaProjects/lumen/src/lumen/completion.py:63)、[停滞记录](/Users/admin/IdeaProjects/lumen/src/lumen/agent_loop/loop.py:754)、[pi 缓存默认](/Users/admin/IdeaProjects/pi/packages/ai/src/api/anthropic-messages.ts:54)。

**7. 建议的改进顺序与验收方法**

1. **先缩减无产出的模型往返。** 在现有 TaskController Interface 上明确允许“完成上一步 + 开始下一步 + 已知参数工作调用”同响应有序发送；普通进度不人为设置复杂验收；有独立下一步工作时避免单独调用 report_progress。证据 ID 等未知结果仍必须等实际工具返回。当前已有合批能力，先用真实任务验证提示与工具说明的效果，再决定是否需要更深的 Interface 调整。
2. **把推理选择变成可观测配置。** 保留未配置时的 Provider 默认；提供明确的当前推理设置与实际映射诊断。由使用者选择速度/深度，在相同模型与路由下比较低 effort 与原设置，记录交付质量、重试、工具错误、输出 tokens；不擅自关闭所有模型思考，也不以减少硬预算掩盖问题。
3. **让交付过程尽早产生可检查产物。** 以早期可用草稿和局部改进减少“整份产物在思考中反复规划”的空间。这属于行为设计，需要真实任务验证；不能强制对所有分析任务提前写文件。
4. **分离真实执行时钟和界面时钟。** 在现有 Gateway/Loop 记录 prepare、approval wait、executor start/end；单工具结束后立即发布执行事实，模型历史仍按批次原顺序追加。加入请求开始、首传输/首 thinking/首 text、流结束、上下文准备/压缩时间。优先用这些指标定位长等待，而非增加另一套调度 Module。
5. **选择性开启安全工具并发。** 核实生产注册的 concurrency 合同后比较 sequential 与 parallel_safe；危险、写入冲突、未知副作用和依赖关系保持既有屏障。不要为了接近 pi 的默认值直接全并发。
6. **最后才优化本地序列化/预算热点和停滞策略。** 先有 profile；若问题确实集中在全历史反复处理，再在 ContextEngine 内缓存稳定 schema/计数等。停滞纠偏应以真实无进展证据为依据，避免把正常重复验证或持续流式推理误杀成失败。

建议用同一组真实任务做配对验证：单文件修改、多文件修复、研究报告；固定模型版本、Provider endpoint、协议、输入材料和验收标准，使用新 Session，并区分冷缓存/暖缓存。依次只改变控制合批、推理设置、工具并发，避免把多个变量一起改变后声称找到了根因。先比较 Lumen 内部各因素，再与 pi 对照；pi 默认四工具与 Lumen 全工具配置可作为产品默认体验比较，但不是纯 Loop 性能实验。

每次至少记录：墙钟时间、首次真实工具执行、首次有效产物、成功交付时间、请求数/尝试数、纯控制批次占比、工作工具与控制工具错误率、thinking 字符/原生 reasoning tokens（若 Provider 提供）、实际输入/缓存/输出 usage、审批等待、压缩时间、最终交付质量和验证结果。不要用“thinking 字数变少”单独证明质量不变，也不要用“最终状态 completed”替代产物验证。

**8. 已做验证与研究限制**

本次实际运行以下定向回归，72 项全部通过：

```bash
uv run pytest -q tests/test_lumen_agent_loop.py tests/test_reasoning_streaming.py \
  tests/test_task_execution_regressions.py tests/test_pydantic_driver.py
```

覆盖原生文字/思考与 usage、完成拒绝、安全重试、空闲/总时限区别、截断工具不执行、并发屏障、取消与回执、证据可用性、思考协议映射及工具参数准备展示。另执行上述 2/8/3 请求的受控 Runtime 实验，均通过。未改 Python/Web 生产代码，所以没有把全量 pytest、Ruff、Pyright、Web build 作为本次已执行验证。

pi 本地没有 node_modules，本次读取源码和契约测试，没有安装依赖或声称运行过 pi 测试；没有真实模型跨框架 A/B、CPU profile 或完整旧任务重跑。原始失败 Session 缺少足以精确分离 Provider 计算、网络和最后失败阶段的时钟，因此不能给出“优化后一定快多少倍”的结论。

此次只新增本研究文档；没有删除代码。当前尚未提交的 evidence、thinking streaming、计划提示及 usage 修复保持原样。结论要求继续保留原生 Loop 单一重试权威、Session append-only、原生 reasoning 的保真恢复、审批与 Sandbox 正交、未知外部动作禁止自动重放、TaskWorkspace 验证和 AgentOrchestrator 生命周期约束。
