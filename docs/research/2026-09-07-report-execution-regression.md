# HTML 研究报告长耗时与计划进度排查

日期：2026-09-07。依据：用户指定 Session 的只读 journal、当前源码、官方资料、
合成回归与隔离目录中的真实 Qwen 调用。未修改原 Session、用户 YAML 或已有产物。

## 现场结论

Session `a6b48e3d-0556-45f0-b1eb-10d121dbae4b` 使用
`anthropic:qwen3.8-max`，Default 模式。Anthropic 是协议 Adapter，实际模型是 Qwen。

| 证据 | 结果 |
| --- | --- |
| 开始、失败时间 | 06:18:45–07:15:04 UTC，约 56 分 18 秒 |
| 模型请求 | 23 次，22 个完成的工具响应批次 |
| 原生思考 | 已完成消息的 ThinkingPart 合计 176,424 字符；timeline 有 15,800 个 ThinkingDelta |
| 输出 usage | 67,025 tokens，包含思考等模型输出，不是 HTML 正文字数 |
| 工具 | 31 次调用，7 次错误；原 usage.tool_calls 错记为 0 |
| 工具计时 | 逐工具耗时之和约 62 秒；混有同批等待/审批时间，不能当作独占墙钟耗时 |
| 交付 | 只有证据 Markdown、图表脚本与几何 JSON，没有执行 HTML 写入 |
| 终态 | failed；计划为 research/structure/charts completed、write in_progress、verify pending |

这次并没有“全部完成”。最后的 RunFailed.message 是空字符串，journal 没有保留
原异常类型或原始 SSE，不能进一步断言最后一次失败是超时、配额、截断或 Provider
内部断言。现有证据也不支持靠提高请求数/工具数上限来修复。

## 根因与对应修复

1. **未配置思考不等于关闭思考。** 用户配置只有 max_tokens=131072。百炼 Qwen3.8
   混合模型默认思考；其 Anthropic 接口的 max_tokens 不限制思考长度。
   另外，当前 SDK 把通用 thinking=False 翻译为省略字段，仍会继承百炼默认。
   初次修复曾为 Qwen Anthropic 路由注入默认 thinking=False；用户复核后明确要求
   恢复模型原默认，现已删除这段归一化。只有用户显式配置 thinking=False 时，
   Driver 才发送 thinking.type=disabled；不按模型名自动开关思考。
2. **验收证据只有执行器知道，模型拿不到。** Runtime 创建随机 evidence receipt ID，
   但模型工具结果没有该 ID。研究步骤两次完成被拒；模型分别猜了 Work Product ID
   与 artifact hash 来 link_evidence，均失败，随后重写目标和步骤、删除验收条件。
   因此第二份计划是模型真实重建，而不是两份独立前端状态。
   现在有验收条件时，成功工具结果的模型投影附带 plan_evidence；原始 UI/audit 结果
   保持不变。完成失败会提示 link_evidence 的真实可用 ID，Session 恢复上下文也包含
   条件和有界证据目录。缺少证据仍阻止完成。
3. **普通进度被过度形式化。** 原控制提示要求任意文件修改或单条命令都先规划；
   模型还能为纯思考准备步骤生成无法由工具验证的条件。现在只为实质多阶段工作
   建立一份短清单，schema 明确普通步骤和纯思考步骤的 acceptance_criteria 留空，
   验收只用于可由执行结果验证的要求；大产物尽早写出可用版本再局部完善。
   这是行为指引，不能保证任何模型绝不多规划或延迟更新。
4. **计数含义不清。** 胶囊原先显示当前步骤的序号，详情显示已完成数量；多个步骤
   同时进行或首步验收受阻时会长时间停在 1。现在统一明确显示“已完成 N / M”，
   当前动作标题仍由真实计划状态投影，不按时间或工具次数伪造进度。
   无计划时的普通工具证据不再发出空 PlanUpdated，避免产生空计划条目。
5. **失败诊断丢失。** Driver 和 Loop 对空错误保留异常类型/类别作为消息兜底，
   Runtime 也保证失败提示非空；成功、等待和失败路径的工具 usage 使用实际调用计数。
   不把未知失败包装成 usage limit，不对未知异常一律自动重试。

初始两次 set_plan 还把 constraints 数组传成了 JSON 字符串，report_progress 有一次
超过 800 字符。保留严格校验，在工具说明中明确数组类型；未增加宽松反序列化来
掩盖不合规调用。

## 与公开实现对照

Agent Loop 负责执行协调；模型推理深度是另一个配置维度。不存在“三者都默认关闭
思考，所以 Agent Loop 不需要推理”的统一实现。

| 产品 | 本次可核验事实 | Lumen 采用的原则 |
| --- | --- | --- |
| Codex | model_reasoning_effort 与 reasoning_summary 分开；推理强度依模型支持情况配置 | 隐藏思考展示不等于减少思考计算 |
| Claude Code | effort 与 thinking 有独立控制；模型可有不同默认和始终思考限制；第三方接口省略字段仍可能思考 | 明确发送参数，不把省略字段当成 off |
| pi | 当前公开 defaults.ts 为 medium；SDK 恢复 Session/模型/用户设置并按模型能力约束 | 不从旧版本印象推断当前默认，尊重模型差异 |
| 百炼 Qwen3.8 | Max/Flash 默认开启混合思考，Anthropic 兼容接口支持 disabled | 保留模型默认，仅按显式配置开关 |
| DeepSeek V4 | Flash/Pro 默认开启思考、effort 为 high；Responses 用 reasoning.effort 控制 | 默认不覆盖，原生参数优先；保留思考流及工具回传 |

官方依据：

- [Codex 配置](https://developers.openai.com/codex/config-reference/)
- [Claude Code 模型与思考设置](https://code.claude.com/docs/en/model-config)
- [pi 当前默认](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/defaults.ts)
  与 [SDK 解析顺序](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/sdk.ts)
- [百炼思考模型](https://help.aliyun.com/zh/model-studio/deep-thinking/)
  与 [Anthropic 参数语义](https://help.aliyun.com/zh/model-studio/anthropic-api-messages)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
  与 [Responses API](https://api-docs.deepseek.com/zh-cn/api/create-response/)

## 思考与工具过程复核

用户反馈“思考模块不推送”后，检查了配置、Driver、Loop、Runtime、Host 事件及 TUI/Web 投影。
Agent Loop 不拥有独立的模型思考开关；原生模型推理、公开调研说明和真实工具执行是不同事件。

- 删除了初次修复中人为关闭 Qwen 的默认设置；用户配置文件从未被改写。
- 发现原有 Responses 兼容缺口：SDK 将 reasoning_text 存到 provider_details.raw_content，
  Lumen 仅看 content/content_delta，因而模型可能持续思考但没有 ThinkingDelta。
  Driver 现投影 raw_content 文本增量，不改写 canonical response，不展示不透明元数据，
  工具结果回传、持久化和恢复仍保留原生 reasoning item。
- SDK 对未知模型 profile 会静默忽略统一 thinking 设置。Responses Driver 对显式设置
  补齐 SDK effort 映射，原生 openai_reasoning_effort 优先；未设置仍保持省略。
- 原有工具准备阶段也有空窗：工具参数可能很长，而 ToolCallStarted 只在完整响应通过
  校验后发出。现追加 LoopToolCallStreaming → ProgressReported，说明正在生成参数、
  尚未执行。不会提前执行、伪造工具完成或增加工具 usage。
- 后续报告 turn 于 08:37:58–08:41:56 UTC 等待约 4 分钟后取消；其 journal 只记录
  RunStarted/RunCancelled，1 次请求、0 工具、0 输出 usage。缺少原始 SSE，不能据此
  断言那一次等待必然是生成工具参数。
- 新增真实接口小测试（未设置 thinking，输出上限 4096，临时测试预算）：DeepSeek V4 Flash
  2.5 秒、404 个思考字符；Qwen3.8 Max 5.9 秒、427 个思考字符。均为 2 次请求、
  1 次工具执行、0 工具错误，依次收到思考、准备提示、工具开始/结束和最终回答。
  这些数字仅证明事件链和默认配置，不代表完整研究报告的耗时。
- 通过 WorkspaceRunLock 确认没有执行中任务后，重启 8765 Web 为后台进程并刷新现有
  Chrome 页面。独立测试 Session `2ca1df62-513d-431b-b875-8405d6b05fae` 分别使用
  DeepSeek V4 Flash 和 Qwen3.8 Max 调用 list_directory，页面均约 5 秒完成；
  现场确认思考、准备进度、工具结果可见，完成后折叠到“已完成处理”，展开仍能查看。
  当前模型选择恢复 Qwen，原报告 Session 历史未改动。后台服务可用 `lumen web --stop` 停止。

## 验证与限制

- MockTransport 检查实际 Anthropic HTTP 请求体：显式 off、low、原生参数优先、
  未配置其他模型时保持省略；不只检查配置对象中的布尔值。
- Runtime 回归：模型从工具结果拿到真实 ID，关联条件、完成步骤、交付；只建一次计划，
  中途保留部分完成状态。验收缺失仍拒绝；空异常可诊断，失败仍保存工具计数。
- ContextEngine 回归：恢复计划的验收条件和证据 ID 对模型可见，且不写入 canonical history。
- 思考复核后的全量 Python 回归 1057 项通过（含 60 个 TUI snapshots）；
  后补原生参数优先的用例后，9 项思考/工具流定向测试全部通过。
  覆盖开关省略、显式关闭、low、原生参数优先、Responses 思考增量与工具结果回传、
  JSON 序列化恢复、参数未完成时的进度与取消、仅执行一次。
- 初次修复时关闭思考的真实 Qwen 小规模测试：临时目录、4,096 输出上限、不检索、不编造患者数据，三步
  生成简短 HTML 研究方案并读回。最终 25.6 秒、11 次请求、10 次工具调用、零 thinking
  字符、零工具错误、计划 revision=1，完成数逐步 0→1→2→3。
  实际文件在临时目录的 outputs/report.html，2,363 字节；检查到完整 HTML/body 和
  背景、影响因素、预测方法、局限性四部分。
- 首轮隔离测试在 36.8 秒触及测试显式设置的 12 次请求上限：HTML 已生成，但准备步骤
  被模型添加的验收条件挡住。该反馈促成 schema 说明修正；最终测试没有人为请求数
  上限，保留独立 180 秒测试进程时限。不能把测试预算耗尽说成生产默认硬墙。
- 全量 pytest 与 60 个 TUI snapshots 通过；Web 142 个测试、TypeScript typecheck、
  Next build 通过。全仓 Ruff 受测试前已有的未跟踪 scripts/chart_geometry.py 的
  44 条错误影响；保留该用户文件，排除它后 Ruff 通过，Pyright 通过。
- 原长任务未重跑，未验证完整医学证据质量，也不能承诺复杂研究都在几十秒完成。
  胶囊改动用组件回归和构建验证；思考与工具过程已在重启后的用户 Chrome 页面复测。

保留原生 Loop 重试权威、未知外部动作恢复限制、审批与 Sandbox、Work Product 验证、
Session 追加写入及旧 schema 加载路径。没有删除生产 Module；删除的仅是强制单步
规划指引、空计划事件和含糊计数展示。它们分别被新指引、原有 Session 证据持久化及
明确的完成数投影替代；不存在第二套进度权威。
