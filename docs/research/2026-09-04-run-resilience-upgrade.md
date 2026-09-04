# 长任务持续执行：实施与验证记录

日期：2026-09-04。状态：当前 checkout 已实施。源码与契约测试是行为权威。

本文保存实施阶段证据；持续维护的技术说明见[长任务持续执行、恢复与排障](../architecture-guide/14-long-running-recovery.md)。

## 踩坑复盘与防回归

| 坑 | 实际影响 | 后续维护依据 |
|---|---|---|
| 总时限被误报为次数超限 | 调高 60/100 无效，活跃生成仍被 300 秒取消 | typed cause 分离；持续输出和显式总时限分别测试 |
| 以可见输出作为禁止重试条件 | 半截文字后只能让用户手动继续 | 撤回候选文字；完整工具批次保留，副作用计数验证 |
| SDK 与 Loop 都重试 | 实际尝试、等待和 usage 不透明 | OpenAI/Anthropic 主模型 SDK retries=0，Loop 统一计数 |
| 用模型语义事件判断 SSE 活跃度 | 心跳被忽略，长思考被误判静默 | HTTP read timeout 感知原始数据；其他 Driver 标准事件计时 |
| 只在用户 turn 之间压缩 | 一个长 run 内仍会超上下文 | prepare_step + 一次有效 overflow 恢复；保留目标与完整批次 |
| 内存 checkpoint 被误当成持久父节点 | 重载出现断链或重复消息风险 | 多次摘要合并；append+fsync 后发布；source_end 去重 |
| 诊断消费者默认每项都是工具记录 | 新 model_attempt/model_retry 导致 name KeyError | 按类型筛选诊断，保留工具审批集成契约 |
| Web 使用 UTF-16 偏移撤回 | emoji 与跨 thinking 段可能错删 | 按 Unicode 码点对齐 Python 字符计数 |
| 仅重建网页版快照 | index.html 仍展示“公开事件后禁止重试”的反向结论 | 同步首页图解与 Markdown，再生成并浏览核验 |

这些经验已通过 MemoryManager 写入 Lumen 原生 project scope 记忆，SQLite 为权威、MEMORY.md 为
审计投影；根 AGENTS.md 保留简短维护记忆。未修改原有用户记忆或自动学习开关。

## 根因与目标

原实现把含持续输出的 300 秒单请求总时限抛成用量超限，同时显示 60/100 次预算提示。
提高次数无法修复这类失败。升级分别处理连接空闲、显式预算、瞬时故障、上下文压力和恢复，
正常长任务无需用户反复点击继续。

参考公开行为：[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、
[Claude Code CLI](https://code.claude.com/docs/en/cli-reference)、
[Claude Code 执行与上下文](https://code.claude.com/docs/en/how-claude-code-works)、
[pi 设置](https://pi.dev/docs/latest/settings)、[pi 压缩](https://pi.dev/docs/latest/compaction)。
Codex 的流空闲计时、Claude Code 的可选 max-turns、运行内上下文压缩是设计参照。
这不构成对闭源 Claude Code 内部算法或所有 Provider 网络表现完全相同的声明。

## 已实施行为

| 场景 | 当前处理 |
|---|---|
| 长时间持续生成 | 默认没有单请求总截止时间；空闲等待随数据到达延续 |
| OpenAI / Anthropic SSE 心跳 | HTTP read timeout 感知原始数据，心跳不会被误认为模型静默 |
| 其他 ModelDriver | 在标准事件读取期间计时，排除 UI 事件消费耗时 |
| 临时连接故障、可恢复 HTTP 错误 | 当前逻辑请求默认最多重试 5 次，指数退避带抖动，记录每次实际尝试 |
| Retry-After | 在允许的最大等待内遵循服务端提示；超过等待预算则交还明确失败 |
| 请求中断前已输出文字 | 撤回当前失败候选文字，再生成；Web/TUI/Timeline 按 Unicode 字符跨 thinking 分段处理 |
| 请求中断前已收集工具参数 | 不执行半截调用；仅完整成功终止的工具响应可交给 CapabilityGateway |
| 前面请求已完成工具批次 | 保留结果；自动重试只重发当前模型请求 |
| Provider 内置工具活动 | 标为不可安全重放，禁止透明重试 |
| 上下文接近窗口 | 每个完成步骤后的模型请求先由 ContextEngine 检查，需要时同轮滚动总结 |
| Provider 返回 context overflow | 一次强制压缩恢复；投影不变或再次超限则停止，不形成无限重试 |
| run 失败/取消后继续 | 已完成模型/工具批次追加到 Session，重载可直接使用其结果 |
| 真正次数预算或总时限 | 仅显式配置时强制执行，错误分类不再混用 |

## 配置与兼容

`LimitsConfig.request_count`、`tool_calls`、`model_request_timeout_seconds` 默认 `None`。
`model_stream_idle_timeout_seconds=300`；`model_retries=5`；
`model_retry_delay_seconds=2`；`model_retry_max_delay_seconds=60`。
根项目 `agent.yaml` 的 60/100 次预算与总时限已改为 `null`，其他配置保留。
原生子 Agent 的请求数、工具数、运行时间默认无硬截止；显式请求/工具预算与父配置取最小值。
旧配置中的有限预算原样有效，不偷偷扩容；旧 delegation Adapter 保留原有兼容契约。

Session 仍为 v9，配置仍为 v2，无历史文件重写。checkpoint 的既有绝对 `source_end`
用于推导当前 turn 被压缩覆盖的前缀；消息原文仍完整保存在 append-only journal。
多个同轮摘要合并成以最后已持久化 checkpoint 为父的候选，只有 turn 追加成功才发布。
失败/取消的完整批次使用有界诊断标记识别，旧失败记录仍按原有审计语义加载。

## Module 权威与删除依据

- LumenAgentLoop 唯一拥有模型尝试、退避、计数及终止状态；没有第二套恢复调度器。
- PydanticAIModelDriver 只翻译传输与事件；OpenAI/Anthropic SDK retries 设为零，消除隐藏的重复重试。
- ContextEngine 新增 prepare_step Interface，复用现有摘要、ArtifactStore、anti-thrash 和 checkpoint 验证。
- Coordinator 仍先追加 Session 再发布状态；SessionRepository 仍只读写持久事实。
- 删除 Runtime 未使用的 `_RETRY_MAX_ATTEMPTS` 常量：生产重试完全由原生 Loop 管理。
- 删除 Loop 的 `provider_retries_before_output` 旧限制：单一新重试策略完全替代其权威；
  相应旧测试改为验证可见文字撤回与安全恢复。
- 未增加新的公开 run 事件；重试通过现有 ProgressReported 与诊断投影到各客户端。

审批、Sandbox、权限收窄、unknown effect 禁止重放、工具超时和 CompletionGate 保持有效。
取消可中断退避，重试耗尽会结束；持续心跳本身不证明任务在推进，但也不会被硬总时限杀死。
未引入无限自动预算续租或不受控重试。

## 验证范围

测试覆盖超过旧默认上限的 106 次模型请求/105 次工具调用、持续输出超过单次空闲时长、
SSE 心跳、静默重连和资源关闭、取消退避、Retry-After、不可重放错误、context overflow、
连续两次同轮压缩后的 checkpoint 重载、失败后会话恢复，以及工具只写入一次。
所有 Provider 测试采用 mock/replay/FunctionModel，没有调用付费模型；生产端到端网络延迟
与 Provider 限额不属于这些离线测试的保证。实际检查结果在本文件末尾记录。

运行中的旧 Lumen 进程必须重新启动以加载新 Runtime；编辑配置不能替换已加载的 Python 类。

最终检查结果：

- `uv run pytest`：1023 passed，60 snapshots passed（78.02 秒）。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- `uv run python -m lumen.contracts --check`：通过；catalog 经正式生成命令更新。
- `uv run python scripts/build_architecture_atlas.py --check`：通过。
- `pnpm --dir src/web test`：19 个文件、141 项测试通过。
- `pnpm --dir src/web typecheck`、`pnpm --dir src/web build`：通过。
- Web OpenAPI 与 TypeScript schema 经 `api:schema` 生成，静态产物经 Next build 更新。
- 本次修改路径的 `git diff --check`：通过。

工作区已有其他修改，均保留；未提交、推送或重启运行中的会话。

## 后续文档与记忆对齐（2026-09-04）

通过 MemoryManager 写入并重新打开 SQLite 核验 6 条项目记忆，保留原有记录；Memory 的敏感内容
检查将长路径字符串识别为不透明内容，最终记忆省略长路径、改用章节名称，未修改校验规则。
AGENTS.md 保存简短维护指引，第 14 章保存持续维护的完整说明，研究记录保留当时的测试证据。
已同步 Context、Session、配置、原生子 Agent、Loop 决策记录、实现审计、README 与命令说明。
Atlas 首页修正旧重试分支、同轮压缩与子 Agent 限制，新增章节入口并更新章节数和核对日期。

此次仅调整记忆和文档：20 份文档的本地 Markdown 链接检查通过，contracts freshness、Atlas freshness、
git diff whitespace 检查通过；浏览器已核验新恢复分支与第 14 章 Trace Reader 的 SNAPSHOT VERIFIED
状态及正文渲染。未将实施阶段的全套测试结果冒充为本次重新执行的测试。
