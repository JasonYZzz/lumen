# 14. 长任务持续执行、恢复与排障

核对日期：2026-09-04。本文描述当前实现；源码与契约测试优先。实施经过与当时的全套验证见
[升级记录](../research/2026-09-04-run-resilience-upgrade.md)。

## 14.1 先识别停止原因

旧报错的具体原因是 `model request deadline reached: 300s`，外层却显示 request_count=60、
tool_calls=100 并建议调大次数。它混淆了不同限制：即使模型一直输出，固定总时限仍会取消请求，
本请求尚未交付的工具调用随之丢弃。只调高次数或把 300 秒改成更大数值不能解决这一设计问题。

| 当前原因 | 处理位置 | 行为 |
|---|---|---|
| 显式请求/工具预算耗尽 | LoopBudgetExceeded | 停止并报告预算，不自动续租 |
| 模型连接空闲或瞬时故障 | Loop 的请求恢复 | 可安全重放且未耗尽重试次数时自动恢复 |
| 显式单请求总时限 | LoopRequestTimeout | 独立错误；不再解释成次数耗尽 |
| Provider 配额、权限或不可恢复错误 | Driver 分类、Loop 终止 | 不通过抬高本地次数绕过 |
| Provider context overflow | LoopContextOverflow + ContextEngine | 一次强制压缩；请求消息不变或再次失败则停止 |
| 不完整/被拒绝的输出、未解决 Effect | Loop / CompletionGate / Host | 不执行半截工具调用，不伪装任务完成 |

## 14.2 默认配置与计数语义

| 配置 | 默认值 | 含义 |
|---|---|---|
| agent.limits.request_count | null | 可选的每 run 逻辑模型请求数；transport 重试不另耗逻辑请求预算 |
| agent.limits.tool_calls | null | 可选工具调用预算，执行整个批次前检查 |
| agent.limits.model_stream_idle_timeout_seconds | 300 | 等待流数据的空闲时间 |
| agent.limits.model_request_timeout_seconds | null | 可选总时限，包含请求内重试；持续数据不刷新它 |
| agent.limits.model_retries | 5 | 首次请求失败后最多重试次数；不是总尝试次数 |
| agent.limits.model_retry_delay_seconds | 2 | 指数退避基础延迟，乘以 0.8–1.2 抖动 |
| agent.limits.model_retry_max_delay_seconds | 60 | 单次退避上限；Retry-After 超过此值不自动等待 |
| agents.request_count / tool_calls / timeout_seconds | null | 原生子 Agent 可选预算；请求/工具数与父配置取更严格值 |

`request_count` 和 `model_attempts` 分开：同一请求首次失败后重试成功是一个逻辑请求、两次实际尝试。
输出上限续写、完成门禁修复与压缩后的新请求会形成新逻辑步骤。usage 累加 Provider 已报告的值，
不把尚未报告的 token 估计为实际账单。尝试/重试诊断为 `model_attempt` / `model_retry`；诊断列表
也含工具记录，消费者不能假定每项都有 `name` 或 `status`。

保留 tool_timeout_seconds=60、output_limit_retries=3 等独立约束。旧配置中的有限值仍生效，
不会自动改写配置或升级为无限；旧 delegation 配置继续经过兼容 Adapter。此次恢复改造保持 Session v9、配置 v2；后续推理选择引入 Session v10（见第 7 章）。

## 14.3 滑动等待与安全重试

OpenAI/Anthropic 使用底层 HTTP read timeout：SSE ping 等原始数据也会延续等待。
其他 ModelDriver 在读取标准事件时计时，事件 sink/UI 消费时间不占空闲等待预算。
持续数据允许单次生成超过 300 秒；只有显式总时限才会限制总耗时。

主模型的 OpenAI/Anthropic SDK retries 设为零，由 `LumenAgentLoop` 统一退避、记录 usage 和处理取消。
这个结论不泛指摘要、标题、Memory 提取等无工具辅助请求。其他 Provider 的 SDK 行为需逐 Adapter 核实。

```text
冻结当前请求 → 开始尝试 → 读取流数据
                           ├─ 完整工具响应 → Gateway 执行 → 保存结果 → 下一请求
                           ├─ 完整最终响应 → CompletionGate
                           └─ 可恢复故障
                                ├─ replay_safe 且有重试额度
                                │    → 撤回本次候选文字 → 退避 → 重试当前冻结请求
                                └─ 不能安全重放或额度耗尽 → 保存 partial outcome → 失败
```

候选文字可见不等于工具副作用已发生。失败响应的工具调用尚未交给 Gateway，可以丢弃并重新生成；
之前完整步骤的工具结果则仍在当前请求历史中，不再次执行。Provider 内置工具活动会把该响应标记为
不可安全重放。未知外部 Effect 仍按现有对账门禁处理。

`TextRetracted` 只撤回相应候选文字；thinking 是独立展示通道，不能拼入最终回答。
Timeline、TUI、Web 跨 thinking 分段撤回，Web 用 Unicode 码点对齐 Python 字符计数。
重试进度复用 `ProgressReported`，不提前发出 `RunFailed`。用户取消可以中断退避。

浏览器到 Host 的 SSE 断线续传与 Host 到 Provider 的模型重试不同：前者按 event sequence 补齐展示，
不重新执行模型；后者由 Loop 处理当前模型请求，不新建 run。

## 14.4 同一 run 内压缩与持久化

`ContextEngine.prepare` 处理 run 起点；`prepare_step` 在后续模型请求前读取当前快照，达到压力阈值
且未处于 cooldown 时总结新增 raw 消息。保留用户目标、近期完整模型/工具批次及瞬时上下文投影。
原有确定性 preflight 缩减仍在最后请求冻结前生效，不能把禁用摘要或固定内容本身超限伪装成可恢复。

多次同轮摘要只产生一个待发布的最新 checkpoint。它以最后已持久化 checkpoint 为父，
source digest 覆盖该父之后的完整 raw 区间；中间摘要不是已提交的父节点。
`covered_new_messages` 是运行内游标，不新增 Session schema 字段。

Coordinator 先 append + fsync，再调用 `confirm_persisted` 发布。Session 重载由绝对 `source_end`
推导本 turn 已被摘要覆盖的消息前缀，active history 只追加未覆盖尾部，full history 保留全部原文。
摘要失败或写盘失败不推进已持久化游标。

失败/取消后，Runtime 将已完成模型/工具批次放入 PartialRunOutcome.completed_messages。
Coordinator 持久化后发布；Session 检查 `completed_model_steps` 数量标记及历史有效性后恢复。
旧失败记录没有此标记，仍为审计材料；不会把半截工具调用提升为可续跑历史。
这不等于进程硬退出时可还原尚未落盘的模型流。

## 14.5 踩坑与维护检查

1. 按 typed cause 排障，不只看外层提示；不要把时间、次数、配额和上下文混为一谈。
2. 识别 SSE 心跳与 SDK 内层重试，否则会出现假静默和不可观测的重复尝试。
3. 验证自动恢复和失败后重载两条路径；结果一致不代表工具只执行了一次，测试必须计数副作用。
4. 区分内存候选 checkpoint 与已持久化 checkpoint；检查连续两次同轮压缩后没有重复/丢失消息。
5. 候选文字撤回覆盖中文、emoji 和 interleaved thinking，避免 Web UTF-16 偏移误删。
6. 只读取脱敏配置或必要字段，不把真实 agent.yaml 凭据写入日志、diff 或记忆。
7. 同步 Markdown、Atlas 首页图解与生成快照；freshness 通过不能证明手写图解语义正确。
8. 运行中的 Python 进程需重新启动才能加载升级；离线测试通过不等于验证了真实 Provider 网络。

## 14.6 回归证据

| 契约 | 测试入口 |
|---|---|
| 超过旧次数上限、滑动空闲、取消、退避、context overflow | tests/test_lumen_agent_loop.py |
| SDK retries=0、Retry-After、SSE ping、协议翻译 | tests/test_pydantic_driver.py |
| 同轮压缩、checkpoint 与原始消息重载一致 | tests/test_context_engine.py |
| 写入后断流自动恢复、失败重载不再次写入 | tests/test_run_coordinator.py |
| partial usage 与显式总时限独立错误 | tests/test_runtime.py |
| 默认无限与旧有限配置兼容 | tests/test_config.py |
| 中文/emoji 跨 thinking 撤回 | tests/test_timeline.py、tests/test_tui.py、Web run-reducer.test.ts |

实施阶段的全套结果：1023 项 Python 测试、60 个界面快照、141 项 Web 测试通过。
这些是 2026-09-04 的离线验证证据，不是持续更新的测试总数或跨产品完全等价声明。
