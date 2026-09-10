# 推理强度与子 Agent 并发：Lumen / pi 源码复核

> 后续已按本报告落实代码优化；当前状态与实验入口见[交付说明](2026-09-08-reasoning-optimization-delivery.md)。下文保留研究时基线。

日期：2026-09-08。Lumen 基线 `250adf89903761260413577848519e818b825f16` 加当前工作区修改；pi 基线 `b2602be77cb7b0de45dd616407fd210daa48aa75`，工作区干净。本文是研究与优化建议，不是 Accepted 实现规范。本轮不修改生产代码，不访问真实模型，不修改用户配置或原 Session。

**结论：应该借鉴 pi，将推理强度做成统一、可校验、可显示、可恢复的应用能力。Lumen 已有底层传参和子 Agent 档位字段，不需要再造模型 Loop。原生子 Agent 已默认有界并发，主要缺口是模型如何使用它、配置是否真正生效，以及异步路径中的阻塞操作。**

## 1. 先纠正两个容易混淆的说法

1. “Lumen 没设 thinking”不等于“没有推理强度能力”，更不等于“关闭推理”。当前省略字段保留 SDK / Provider 的默认行为；本地不能从省略推断服务端的具体 effort。
2. “pi 默认并行”首先指一个模型响应中的工具批次。pi 仓库的 subagent 是需要安装的示例扩展，支持 single、parallel、chain 三种明确模式；不是默认把每个用户任务自动拆成多个子 Agent。

当前 Lumen 已经过上一轮优化：`LimitsConfig.parallel_tool_calls = "parallel_safe"`，Loop 默认允许安全并发，结果完成时即可发布。此前对照报告中的“默认 sequential”和“批次全部结束后发布结果”描述的是优化前状态。

| 维度 | pi 本地实现 | Lumen 当前实现 |
| --- | --- | --- |
| 应用初始推理档位 | coding-agent 无更高优先级设置时 medium；Agent core 自身默认 off | 主模型 settings 默认空，继承上游行为 |
| 推理配置 | 类型化档位、模型支持集、Session 事件、CLI/TUI/RPC | 通用 settings 透传；child profile 有 reasoning_effort；Live 有独立字段 |
| 同一响应内的工具 | 默认 parallel；任一 sequential 工具使整批串行 | 默认 parallel_safe；仅显式安全调用分组并发，exclusive 为有序屏障 |
| 子 Agent | 可选示例扩展；独立 pi 进程；tasks 数组模式最多 8 个、同时 4 个 | 原生 AgentOrchestrator；默认启用，最多 8 个/root run、同一 Session 同时 3 个 |
| 父 Agent 等待方式 | parallel 模式 await 整组完成后返回工具结果；中间可流式更新 UI | spawn 立即返回 Thread；父 Agent 可继续工作；wait_agent 可等待任一目标状态变化 |
| 写入隔离 | 示例默认 cwd 继承父目录；进程隔离不等于工作区隔离 | explorer 共享只读；worker/default 使用独立 Git worktree，导入有检查 |

## 2. Lumen 已有的推理能力与缺口

### 2.1 已有能力

- 主模型：`ModelSettingsConfig.settings` 透传到 Runtime/Driver。今天即可在所选模型的 settings 下写 `thinking: medium`，不必等待新增界面。
- 本地 PydanticAI 的统一类型支持布尔值及 `minimal/low/medium/high/xhigh`；省略、true、false 是不同语义。`max` 不在当前 SDK 的统一 effort 类型中。
- 子 Agent：`AgentProfile.reasoning_effort` 和 `AgentConfigSnapshot.reasoning_effort` 已存在。Markdown frontmatter 使用 `reasoning-effort`；内建 explorer/worker/default 都未设置角色档位。
- 实时语音：`LiveConfig.reasoning_effort` 默认 low，仅属于 Live 通道，不能当成文本主 Agent 的默认设置。
- Driver 已修复 Anthropic 显式 false 的 disabled 传参，以及 SDK 未识别的 OpenAI-compatible Responses 模型对显式 thinking 的兼容映射。

源码：[模型配置](../../src/lumen/config.py:45)、[Live 配置](../../src/lumen/config.py:505)、[角色字段](../../src/lumen/agents/types.py:63)、[角色加载](../../src/lumen/agents/profiles.py:105)、[Driver](../../src/lumen/agent_loop/pydantic_driver.py:283)、[Runtime 初始化](../../src/lumen/resources.py:1351)。

### 2.2 可复现的问题

**A. 角色覆盖可能被原生参数遮蔽。**

Factory 先复制所选模型的 settings，再把 profile 的 effort 写成统一 thinking。SDK 明确让原生字段优先。因此如下组合最终发送 high：

```yaml
# 模型 settings
openai_reasoning_effort: high
# 子角色的 reasoning-effort: low 被 Factory 转为
thinking: low
```

这不是“模型不听指令”，而是配置解析的实际结果。已有行为作为旧透传契约可以理解；作为新用户可见档位的实现则会产生误导，应优先修复。

**B. 未知 Chat 模型可能丢弃统一参数。**

SDK `Model.prepare_request()` 仅在模型 profile 宣告 supports_thinking / thinking_always_enabled 时采用统一 thinking，之后无论是否采用都会移除该 key。Lumen 当前补偿仅覆盖 Responses，不覆盖全部 Chat-compatible 路由。

MockTransport 实测，虚构的 SDK 未知模型 `custom-reasoner`：

| 路由 | 传入 settings | 最终 HTTP 正文中的推理字段 |
| --- | --- | --- |
| Responses | `{}` | 省略 |
| Responses | `thinking: medium` | `reasoning: {effort: medium}` |
| Responses | `thinking: low` + `openai_reasoning_effort: high` | `reasoning: {effort: high}` |
| Chat | `thinking: low` | 省略 |
| Chat | `openai_reasoning_effort: low` | `reasoning_effort: low` |

这只证明本地序列化行为。不同兼容 Provider 接受哪些字段和档位仍需对应能力声明或契约验证，不能对所有 Chat 路由强行发送 reasoning_effort。

**C. 缺少应用级闭环。**

目前主模型配置是 `dict[str, Any]`；没有主文本 Agent 统一档位校验、可用档位清单、`/thinking` / CLI 开关和统一 Host 命令。显示 thinking 流的 UI 与控制推理强度是两件事。最近新增的 `configured_thinking` 诊断记录的是请求准备时的配置，不是经过全部 SDK 映射后的 HTTP 参数，更不是服务端真实推理预算。

**D. 子 Agent 快照还未冻结完整的有效推理选择。**

`snapshot()` 记录角色的可选 effort；执行时再从 model_registry 取 settings。角色省略时，只能从模型配置得到默认，而快照中的 effort 仍为 None。恢复后若配置变更，不能仅凭该字段还原原请求档位。引入 Session 档位切换时，必须同时补全父配置继承、角色覆盖和 spawn 时冻结，不能只改 UI。

源码：[snapshot](../../src/lumen/agents/runtime_factory.py:106)、[child settings 合并](../../src/lumen/agents/runtime_factory.py:419)、[诊断字段](../../src/lumen/runtime.py:1461)。SDK 证据位于本地 `.venv/lib/python3.12/site-packages/pydantic_ai/settings.py` 的 thinking 定义、`models/__init__.py` 的 prepare_request，以及 `models/openai.py` 的 _translate_thinking。

## 3. pi 真正值得借鉴的设计

其实现链路是：应用选择档位 → 按模型能力解析 → Agent state → 每次请求的 reasoning 选项 → API Adapter。

- 初始化优先考虑显式 SDK options，其次恢复 Session，随后按模型覆盖/全局默认兜底，最终使用 medium 并按模型能力限制。模型切换也重新考虑目标模型偏好。
- 统一候选集为 `off/minimal/low/medium/high/xhigh/max`。不支持推理的模型只允许 off；xhigh/max 必须显式出现在模型映射中。不是所有模型都有七个档位。
- `AgentSession.setThinkingLevel()` 更新有效状态，变化时追加 Session record 和事件；只有显式 persist 才写全局默认。
- CLI `--thinking`、TUI `/thinking`/快捷键、RPC set/cycle/get_available 共用 Session 能力，而不是各入口自己改 Provider payload。
- 请求时按 Provider 实现做 effort 或 token budget 映射。预算型路径的默认预算为 1024/2048/8192/16384；adaptive 路径使用 effort。这些数字不是所有 Provider 对 medium 的共同定义。

需要保留两项差异：

1. pi 的 clamp 可能向更高支持档位调整；Lumen 对用户明确要求 off 的场景不应悄悄打开推理。明确说明“不支持关闭”或拒绝该选择更可预测。
2. pi 本地 Anthropic Adapter 的 `supportsMidConvoEffort` 特殊分支固定发送 adaptive + high。**因此应用状态显示 medium，也不能一概断言每个 pi HTTP 请求都是 medium。**对齐性能实验必须看解析后的发送参数。

pi 源码：`packages/coding-agent/src/core/defaults.ts:3`、`core/sdk.ts:229`、`core/agent-session.ts:1793`；`packages/ai/src/models.ts:915`、`api/simple-options.ts:57`、`api/anthropic-messages.ts:850` 与 `:1124`；`packages/coding-agent/src/modes/rpc/rpc-mode.ts:499`、`src/cli/args.ts:147`。所有路径相对于 `/Users/admin/IdeaProjects/pi`。

## 4. 并行实现：哪些已经具备，哪些确实需要改

### 4.1 pi 工具并行

`Agent.toolExecution` 默认 parallel。Loop 先检查整个工具批次是否有 sequential 工具，有则整批串行；否则准备调用后通过 `Promise.all` 执行。执行结束事件可以先发出，模型历史中的结果仍按原调用顺序记录。

read 等独立操作能受益。write/edit 还使用进程内按路径排队的 `withFileMutationQueue`，同路径互斥、不同路径可并发。该内存 Map 不跨 pi 子进程共享，不能把它当作 subagent 示例的跨进程文件锁。

源码：pi `packages/agent/src/agent.ts:237`、`agent-loop.ts:418` 与 `:547`；`packages/coding-agent/src/core/tools/file-mutation-queue.ts:4`。

### 4.2 pi 子 Agent 示例

调用 `{tasks: [...]}` 才选择组内并行；`{chain: [...]}` 使用 for/await，前一步结果替换 `{previous}` 后再执行下一步。

并行路径使用最多 4 个 async worker，共享 nextIndex 从最多 8 个任务中领取工作，再 `Promise.all(workers)` 汇合。每个任务 `spawn` 一个 `pi --mode json -p --no-session` 子进程，通过 stdout JSON 收集结果/usage，转发中间更新，取消时终止进程。未指定角色模型时继承派发 Session 的模型与 thinking；指定角色模型时不再传父 thinking，而由子进程为所选模型解析设置。

默认 cwd 仍是父 cwd，除非任务传入其他 cwd。示例并不自动创建 Lumen 式 worktree 或提供同等的持久恢复、导入验收。

源码：pi `packages/coding-agent/examples/extensions/subagent/README.md`、`index.ts:32`、`:215`、`:300`、`:346`、`:567`、`:646`。

### 4.3 Lumen 已有真并发

`AgentsConfig` 默认 enabled=true、autonomy=adaptive、max_concurrency=3、max_agents_per_run=8。`spawn_agent()` 追加事实、调用 `_schedule()` 后返回；`_schedule()` 使用 `asyncio.create_task()`；`_execute()` 在每 Session 的 Semaphore 中执行 child runtime。

```text
父 Agent 一次响应给出 spawn A、spawn B、spawn C
  → 按顺序登记 A/B/C，各自立即进入后台调度
  → A/B/C 的模型请求及工具工作可以重叠
  → 父 Agent 继续自己的独立工作
  → 需要依赖结果时 wait_agent([A, B, C])
  → 消费已完成结果；若仍有活动目标，再等待剩余目标
```

Agent 控制工具的 `sequential=True` 保证登记、生命周期状态修改有序，**不要求等到子 Agent 完成才调用下一个 spawn**。无需为并行执行把这些控制工具全部标成 parallel_safe。

本地实验使用真实 Orchestrator、临时 SessionRepository 与模拟执行 Factory，连续 await 四次 spawn；前三个执行被 Event 暂停，断言状态为三个 running、一个 queued，释放后四个全部 completed，观测最大活动数为 3。没有启动实际模型、工作区写入任务或新的 Codex 子 Agent。

源码：[默认值](../../src/lumen/config.py:123)、[spawn](../../src/lumen/agents/orchestrator.py:128)、[调度器](../../src/lumen/agents/orchestrator.py:496)、[wait](../../src/lumen/agents/orchestrator.py:277)、[工具注册](../../src/lumen/resources.py:1267)。

### 4.4 真正影响并行收益的剩余问题

- **派发形态。** 如果模型 spawn A 后立即 wait A，完成后才 spawn B，默认并发度再高也没有效果。当前指令仅泛化地鼓励有收益时委派；需要直接示范“先派发已知独立任务、再做本地工作、依赖时等待”。
- **同步 Git。** `NativeAgentRuntimeFactory._execute_worktree()` 的准备和收尾在 async 方法中直接调用 `_git()`；其实现是 `subprocess.run(timeout=120)`。spawn 前的 parent_dirty_hash 也走同步 Git。大仓库/慢 Git 会阻塞同一事件循环中的模型流、其他 Agent 和取消处理。这是源码可确认的实现问题，但本轮没有生产性能数据量化它对历史长任务的占比。
- **等待目标。** wait_agent 对任一已非 queued/running 的目标立即返回。如果反复把已完成目标放回等待列表，会产生无意义的即时返回；优先改工具说明和模型使用契约，确有需求再扩展已有 wait Interface，避免新增轮询器。
- **“adaptive”没有自动任务拆分算法。** 代码里的策略名不是依赖分析器。当前 `explicit` 与 `adaptive` 在 spawn 校验中也没有独立的许可上下文判断，基础指令仍是同一份 adaptive 指引；若将这些选项展示为严格行为保证，需要补相应 Host/运行契约，而不是只靠名称。

源码：[派发指引](../../src/lumen/resources.py:124)、[worktree 路径](../../src/lumen/agents/runtime_factory.py:362)、[阻塞 Git](../../src/lumen/agents/runtime_factory.py:699)。

## 5. 建议的实现顺序

### P0：让推理选择可靠生效

1. 在现有模型配置 Module 增加类型化的应用档位，区分 `provider_default` 与 `off`；以 low/medium/high 为常用选择，minimal/xhigh/max 按能力暴露。不要把字符串 `off` 直接透传给当前 SDK 的统一 thinking，也不要把 `max` 当作它已有的统一值。
2. 在现有模型构建/Driver Seam 统一解析 requested → effective → Provider 参数。能力来源优先已知 SDK profile，未知兼容端点允许经过校验的显式覆盖；不按模糊模型名猜测。避免复制一套完整 Provider SDK。
3. 旧 settings 保留透传兼容：没有新档位时保持旧行为；同一配置层混用新档位和冲突原生字段时报可定位错误。Session/角色显式选择若覆盖更低层设置，解析器必须替换对应冲突键，不能只附加 thinking。
4. child spawn 冻结解析后的有效选择及必要的安全配置来源；角色显式档位优先，否则同模型继承父级有效选择；角色改模型时按目标模型配置/能力重算。恢复不能意外从新配置漂移，不能把任意含秘密 settings 整包持久化。
5. 增加已解析的发送档位、映射原因、来源诊断；仍明确它是客户端发送参数，Provider 未回显时不声称知道实际内部预算。

### P1：应用入口与合理默认

- 新初始化配置/示例对确认支持推理的模型明确设置 medium；旧配置缺省继续 provider_default，不静默改变已有用户成本和行为。优先用现有 config v2 的可选字段与初始化模板表达；若持久记录契约需要升级，按 Session schema 规则追加迁移，不重写旧 header。
- CLI `--thinking`、TUI `/thinking`、Web 档位选择共用 Host 命令和能力查询。显示“Provider 默认 / medium / 不支持 / 无法确认映射”，而不是仅显示一个未经解析的标签。
- 第一版与当前模型切换契约一致，只在运行空闲时修改，下次运行生效，避免无审计地修改正在流式生成的请求；之后若支持运行中切换，通过 Coordinator 排队到请求间并追加事实。
- 通用主 Agent 建议 medium 起步；低复杂度检索/格式处理可配置 low，高复杂度设计/疑难排查可由用户选择 high。不要在每次工具返回后再发一轮“评估推理强度”的模型请求。
- effort 和输出预算不是同一个开关。预算型 Provider 需要为回答保留空间，但不能为了开启 thinking 偷偷放大用户显式 max_tokens；继续使用 ContextEngine 的输出预留和 Loop 的现有恢复契约。

### P1：提高现有并行的实际使用率

- 保持当前安全工具默认并发及子 Agent 默认并发 3。先改派发/等待说明和回归用例，不新增独立的批量调度器。
- 将 worktree Git 操作接入可取消的异步执行 Seam，正确清理子进程；对共享 Git 元数据写入、导入保持必要的串行化。简单套 to_thread 不能解决取消后 Git 仍运行的问题。
- 默认并行适用于已经明确独立的工作，如多个只读调查；依赖链、同目标写入和导入验收继续按顺序处理。小任务不为凑并发而拆分。
- 如果真实轨迹仍大量逐次派发，才考虑给现有 spawn Interface 增加批量入口；必须复用同一个 AgentOrchestrator 和单任务幂等规则，不能新增另一套任务队列。

### P2：用可比较的实验决定是否进一步提速

同模型、同任务、同输入、同 Provider，比较 provider_default / low / medium / high，以及子 Agent 并发 1 / 3。覆盖简单问答、单文件修复、多源调查、独立多文件任务、严格依赖链；重复执行并同时比较交付正确率。

测量总耗时、模型请求数、纯控制批次比例、首事件延迟、每次请求耗时、Provider 返回的 reasoning/output tokens、重试、工具执行/队列/审批耗时、Agent 峰值并发、worktree 准备/导入耗时、总费用和返工次数。thinking 字符数仅是流的规模指标，不等于推理 token 或质量。

即使可独立的三个子任务理想上把执行阶段从 `TA+TB+TC` 降到 `max(TA,TB,TC)`，总时间仍包括父 Agent 分解、派发、汇总与验证；多个 child 还可能增加 token 和并发限流。medium 是可预测的起点，不是“必然更快且同质量”的实测结论。

## 6. 本轮验证与范围

- 已运行 `uv run pytest -q tests/test_reasoning_streaming.py tests/test_pydantic_driver.py tests/test_delegation.py tests/test_orchestration_m6_m9.py`，42 个测试通过。
- MockTransport 验证了上表 5 种实际 Driver HTTP 序列化结果；无网络和付费调用。
- 真实 Orchestrator + 临时 journal + 模拟执行 Factory 验证了四次顺序 spawn 的默认三并发和排队/完成状态。
- 本轮仅新增研究文档并给前篇历史报告加当前状态提示；没有实现新的推理 UI、修改生产默认档位、改变子 Agent 调度器或宣称真实任务已加速。
