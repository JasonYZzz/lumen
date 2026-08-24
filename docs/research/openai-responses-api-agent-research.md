# OpenAI Responses API 面向 Agent 开发的官方技术调研

> 调研日期：2026-08-21  
> 范围：OpenAI 官方开发者文档与 API Reference，以及采用 Responses shape 的 provider 官方兼容
> 文档。本文只采用一手资料，不把社区文章作为证据。

## 结论摘要

1. **Responses API 是 OpenAI 当前推荐的新一代生成 API surface。** 官方把它定义为
   Chat Completions 的演进版本，并明确写明：Chat Completions 仍受支持，但所有新项目都推荐使用
   Responses。调用入口是 `POST /v1/responses`；这里的 `v1` 是 OpenAI REST API 路径的一部分，
   官方没有把它称为“Responses v1 协议”。
   [迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)
2. **对需要 reasoning、多轮工具调用和长任务的 Agent，Responses 比 Chat Completions 更合适。**
   它把 reasoning、message、function call、tool call 等建模为独立的 typed Items，可在轮次间保留
   reasoning 与工具上下文，并原生接入 web search、file search、computer use、code interpreter、
   remote MCP 等 hosted tools。
   [迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)
   [Using tools](https://developers.openai.com/api/docs/guides/tools)
3. **优势不仅是请求字段更“新”，而是 Agent loop 的信息模型更完整。** OpenAI 公布的内部评测称，
   相同 prompt 和 setup 下，reasoning model 通过 Responses 调用在 SWE-bench 上提高 3%；内部测试还称
   cache utilization 相比 Chat Completions 改善 40%–80%。这些数字是 OpenAI 的内部测试结果，
   不能替代 Lumen 自己的质量、成本和延迟评测。
   [Responses benefits](https://developers.openai.com/api/docs/guides/migrate-to-responses#responses-benefits)
4. **Responses 是 OpenAI 的 API Interface，不是跨厂商 Agent 行业标准协议。** MCP 是其中可连接
   工具和数据的独立协议；Responses 本身则规定 OpenAI 模型请求、Items、事件、状态和工具调用的
   API shape。因此，“采用 Responses”与“所有 provider 都原生兼容”是两个不同命题。
5. **升级不等于把 URL 从 `/chat/completions` 改成 `/responses`。** 应同时迁移请求字段、typed
   `output`、function call/output、Structured Outputs、streaming events，以及多轮状态策略；否则会
   丢 reasoning 或工具 Items，或者继续以旧 Message 假设错误解析结果。
   [官方迁移清单](https://developers.openai.com/api/docs/guides/migrate-to-responses#incremental-rollout-checklist)
6. **Responses 不是所有工作负载的唯一入口。** Chat Completions 目前仍受支持，可以逐流量迁移；
   Responses 当前不提供 Chat Completions 已有的音频输出，也不提供 `n` 个并行 generation；实时语音
   Agent 应使用 Realtime API。复杂应用还需在“直接使用 Responses、自主拥有 loop”与“使用 Agents
   SDK、由 SDK 运行 loop”之间做选择。
   [迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)
   [Agents SDK vs. Responses API](https://developers.openai.com/api/docs/guides/agents#agents-sdk-vs-responses-api)
   [Realtime API](https://developers.openai.com/api/docs/guides/realtime)

## 1. 名称、入口与“协议”措辞

官方称 Responses API 为新的 **API primitive** 和用于构建 agent-like applications 的 unified
interface。最小 Python 调用形态是：

```python
from openai import OpenAI

client = OpenAI()
response = client.responses.create(
    model="gpt-5.6",
    input="Solve this task.",
)
print(response.output_text)
```

对应 HTTP endpoint 是 `POST /v1/responses`。与旧接口的核心名称差异是：

| Chat Completions | Responses |
| --- | --- |
| `POST /v1/chat/completions` | `POST /v1/responses` |
| `messages` | `input` / Items |
| `choices[0].message.content` | typed `output` / SDK `output_text` helper |
| `response_format` | `text.format` |
| stream chunks + `delta` | typed server-sent events |
| 应用手工管理 message history | 手工 Items、`previous_response_id` 或 Conversations API |

来源：[Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)

截至 2026-08-21，DeepSeek 已提供 `/responses`，支持 typed input/output Items、function tools、
reasoning text 与语义化 SSE；但它是无状态的部分兼容实现，不支持 `previous_response_id`、
Conversations、`store`、background 等 OpenAI 托管状态能力。不支持的顶层参数会被静默忽略。
因此 provider 能否选择 Responses 应按当前官方能力判断，不能再由“存在自定义 `base_url`”推断为
只能使用 Chat Completions。

Kimi Code CLI 同时提供 `openai`（Chat Completions）和 `openai_responses` provider 类型，说明客户端
配置层应把两条协议建模为显式选择，而不是把 Chat 固定为第三方端点的隐式默认。

来源：[DeepSeek Responses API 兼容性明细](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)
[Kimi Code provider 配置](https://www.kimi.com/code/docs/kimi-code-cli/configuration/providers.html#openai-responses)

严格来说，Responses 是 OpenAI 的请求/响应 Interface 和事件模型，而不是由独立标准组织发布、供各厂商
共同实现的“Agent 通用协议”。Remote MCP 是 Responses 可用的一类工具连接能力，但 MCP 与 Responses
不是同一层概念。[Using tools](https://developers.openai.com/api/docs/guides/tools)

## 2. 为什么更适合当下的 Agent 开发

### 2.1 Items 比单一 Message 更贴近 Agent 的实际动作

Chat Completions 的主要上下文单元是 Message；Responses 使用 Item union。`message`、`reasoning`、
`function_call`、`function_call_output` 以及 hosted-tool call 都可以是独立 Item。官方指出，Chat
Completions Message 把多个 concern 粘在同一个对象中，而独立 Items 更能表示模型上下文的基本单元。

这对 Agent runtime 的直接价值是：

- 可以区分“模型对用户说的话”和“模型请求执行的动作”；
- function call 与其 output 通过 `call_id` 关联，不必伪装为普通 assistant/tool 文本；
- reasoning 和工具上下文可以被保留、回放或压缩，而不必把它们降格成聊天文本；
- stream consumer 可按 event `type` 处理 text delta、function arguments、完成和错误事件。

来源：[Messages vs. Items](https://developers.openai.com/api/docs/guides/migrate-to-responses#messages-vs-items)
[Streaming migration](https://developers.openai.com/api/docs/guides/migrate-to-responses#update-streaming-consumers)

### 2.2 对 reasoning model 的多轮连续性更完整

Responses 能在多轮之间保留 reasoning 与 tool context。官方将这一点与更高模型智能、更少 reasoning
tokens、更高缓存命中率和更低延迟联系起来；当前迁移页给出的量化结果是同 prompt/setup 下 SWE-bench
提升 3%，cache utilization 在内部测试中提高 40%–80%。

这一优势尤其适合 Agent，因为一次任务通常不是“用户消息 → 最终文本”的单轮映射，而是多次观察、
推理、工具调用、结果验证和继续推理。保留前一轮 reasoning/tool Items，可以减少模型每轮重新构造中间
状态的工作。

来源：[Responses benefits](https://developers.openai.com/api/docs/guides/migrate-to-responses#responses-benefits)
[GPT model migration guidance](https://developers.openai.com/api/docs/guides/latest-model)

需要保留两个限定：

- 这些改善是 OpenAI 的内部结果，具体工作负载必须用自己的 eval 验证；
- `previous_response_id` 只是简化状态传递，不会免除前序上下文计费，链上的历史 input tokens 仍按
  input tokens 计费。

来源：[Statefulness and billing](https://developers.openai.com/api/docs/guides/migrate-to-responses#decide-when-to-use-statefulness)

### 2.3 Agentic loop 与 hosted tools 成为一等能力

官方将 Responses 描述为 “agentic by default”：模型可以在一个请求跨度内调用多个工具。当前 hosted
能力包括 web search、file search、computer use、code interpreter、image generation 和 remote MCP，
同时也支持应用自定义 function tools。

与 Chat Completions 相比，Responses 可以直接使用 OpenAI-hosted tools；旧接口需要应用自行实现相应
集成。它减少的是 hosted-tool orchestration 的样板代码，而不是取消应用的安全责任。

来源：[Responses API overview](https://developers.openai.com/api/docs/guides/migrate-to-responses#about-the-responses-api)
[Upgrade to native tools](https://developers.openai.com/api/docs/guides/migrate-to-responses#upgrade-to-native-tools)
[Using tools](https://developers.openai.com/api/docs/guides/tools)

重要边界：对自定义 function，应用仍要接收 function call、执行真实函数、回传匹配 `call_id` 的
`function_call_output`，再调用模型继续生成。Responses 没有替应用自动完成权限检查、审批、幂等、
副作用审计或恢复。

来源：[Responses function-calling flow](https://developers.openai.com/api/docs/guides/agents#agents-sdk-vs-responses-api)
[Function calling](https://developers.openai.com/api/docs/guides/function-calling)

### 2.4 多轮状态有三种明确路径

Responses 为应用提供三种状态携带策略：

1. 应用保存并在下一轮重放完整 Items；
2. 用 `previous_response_id` 链接前一个 response；
3. 用 Conversations API 保存可跨 Session、device 或 job 使用的持久 conversation。

这比 Chat Completions 只能由应用手工维护 Message history 更灵活，但不意味着业务状态权威必须转移
给 OpenAI。需要自有审计、恢复或 provider 可替换性的框架，仍可把自己的 Session journal 作为 canonical
history，并把 Responses state 当作调用优化或 provider projection。

来源：[Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
[Additional differences](https://developers.openai.com/api/docs/guides/migrate-to-responses#additional-differences)

### 2.5 同时支持 stateful 与 stateless/ZDR 路径

Responses 默认存储 response；应用可以设置 `store: false`。对 Zero Data Retention 或主动无状态的工作流，
官方支持 encrypted reasoning Items：应用保存并在下一轮回传加密内容，服务端只在内存中解密使用，然后
丢弃中间状态。

这使“利用 reasoning continuity”和“由应用掌握持久化”不再必然二选一。但 encrypted Item 是 opaque
provider state，不能替代应用自己的可解释事件、工具 receipt 或恢复证据。

来源：[Stateless encrypted reasoning](https://developers.openai.com/api/docs/guides/migrate-to-responses#decide-when-to-use-statefulness)

### 2.6 长任务、长上下文和高频工具循环有对应原语

- **Background mode**：以 `background: true` 异步执行耗时数分钟的推理任务，应用轮询 response 状态，
  避免把长任务绑定在单次 HTTP 连接上。
- **Compaction**：server-side 或 standalone compaction 在减少上下文 tokens 的同时携带后续需要的关键
  prior state/reasoning，以平衡长会话的质量、成本和延迟。
- **Responses WebSocket mode**：在一个持久连接上运行并行 conversation、fork response chain，并只发送
  增量 input；官方称它特别适合多次 model-tool 往返的 coding/orchestration loop，20 次以上 tool calls 的
  rollout 中曾观察到端到端执行最高约快 40%。

这些能力分别解决任务持续时间、context growth 和多轮 transport overhead，不应混为一个机制。

来源：[Background mode](https://developers.openai.com/api/docs/guides/background)
[Compaction](https://developers.openai.com/api/docs/guides/compaction)
[WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode)

## 3. 官方推荐和迁移状态

OpenAI 当前官方表述可以分成三层：

| 对象 | 当前官方状态 | 工程含义 |
| --- | --- | --- |
| Responses API | 所有新项目推荐；构建 OpenAI Agent 的未来方向 | 新的 OpenAI 原生 provider 实现应优先以它为目标 |
| Chat Completions | 仍受支持 | 不是立即停用；可按 user flow 渐进迁移，并保留第三方兼容路径 |
| Assistants API | 2025-08-26 起 deprecated，2026-08-26 sunset | 不应为新 Agent 架构继续投资 Assistants 专有对象模型 |

OpenAI 说明，Assistants beta 的开发者反馈已进入 Responses，使其更灵活、更快、更易用；Responses
代表在 OpenAI 上构建 Agent 的未来方向。

来源：[Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)

## 4. Responses API 与 Agents SDK 的关系

Responses API 是模型交互和工具/状态 Items 的底层 API Interface；Agents SDK 是更高层 runtime。OpenAI
给出的选择原则是：

- 希望应用自己控制 output Items、工具、状态、routing、loop 和 branching：直接用 Responses API；
- 希望 SDK 运行 Agent loop，并提供 handoff、guardrail、session、human review 和 tracing 等 runtime
  能力：使用 Agents SDK。

因此“使用 Responses API”不等于“必须放弃自研 Agent framework”，也不等于 Responses 本身已经提供了
完整的审批、安全、持久化和多 Agent 生命周期。它适合作为 provider Interface 的高能力实现，framework
仍可拥有更上层的 runtime authority。

来源：[Agents SDK vs. Responses API](https://developers.openai.com/api/docs/guides/agents#agents-sdk-vs-responses-api)

## 5. 适用边界与迁移风险

### 5.1 Chat Completions 仍有保留价值

官方允许逐 user flow 渐进迁移。以下情况不应仅因“新版”而强制切换：

- 第三方 provider 只实现 Chat Completions-compatible endpoint；
- 现有稳定工作负载不需要 reasoning continuity、hosted tools 或 Responses 状态能力；
- 需要 Chat Completions 当前已有而 Responses 尚未提供的音频输出；
- 需要 `n` 参数一次返回多个 generation；Responses 已移除该参数，只返回一个 generation。

其中第三方兼容性必须对目标 provider 实测；OpenAI 对自家新项目的推荐不能证明其他厂商已实现相同
API shape。其余能力差异见官方迁移对照。

来源：[Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)

### 5.2 Responses WebSocket 不是 Realtime API

Responses WebSocket mode 面向 text/image、reasoning 和高频工具 loop 的低开销 continuation；实时
speech-to-speech、VAD、插话和音频媒体传输应使用 Realtime API。两者都是 WebSocket 并不表示协议或
Session 语义相同。

来源：[Responses WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode)
[Realtime API](https://developers.openai.com/api/docs/guides/realtime)

### 5.3 状态便利性不等于免成本或免治理

- response 默认存储，部署前应明确决定 `store`、ZDR 和数据治理策略；
- `previous_response_id` 不减少前序 input token 计费；
- hosted/custom tools 仍需要最小权限、审批、副作用追踪、超时、重试与幂等策略；
- opaque encrypted reasoning/compaction Items 不应被当作业务可解释的 canonical journal；
- WebSocket `store=false` continuation 依赖连接内存中的 response cache，断开或缓存缺失时没有持久 fallback。

来源：[Statefulness](https://developers.openai.com/api/docs/guides/migrate-to-responses#decide-when-to-use-statefulness)
[Compaction](https://developers.openai.com/api/docs/guides/compaction)
[WebSocket continuation](https://developers.openai.com/api/docs/guides/websocket-mode#how-continuation-works)

### 5.4 迁移错误通常发生在 output 和 state，而不是 URL

官方列出的高频错误包括：

- 继续读取 `choices[0].message.content`；
- 把每个 `output` Item 都当成 message；
- 手工传递上下文时丢掉 reasoning、function call 或 function output Items；
- function output 缺少正确 `call_id`；
- 继续使用 `response_format` 而不是 `text.format`；
- 复用 Chat Completions stream chunk handler，而不处理 typed Responses events；
- 误以为 `previous_response_id` 会免除历史 token 计费。

来源：[Common migration errors](https://developers.openai.com/api/docs/guides/migrate-to-responses#check-common-migration-errors)

## 6. 对 Agent framework 的采用判断

以下是基于官方能力做出的工程判断，不是 OpenAI 的产品承诺：

1. 若 framework 自己拥有 Session、context、tools、approval 和 multi-agent lifecycle，最合适的定位是把
   Responses 实现为一个高能力 provider Interface，而不是让 provider 反向成为 framework 的唯一状态权威。
2. provider-neutral 的内部事件模型若已经能表示 reasoning、message、tool call、tool result 和 completion，
   Responses typed Items 可较自然地映射进去；若内部模型仍只有 role/content Message，则迁移收益会被兼容层
   压扁。
3. `previous_response_id`、Conversations API、encrypted reasoning 和 compaction 应作为明确可选策略，不能
   同时形成多套不一致的 runtime authority。
4. 应保留 Chat Completions Adapter，直到目标第三方 providers 的 Responses 支持和功能对齐经过契约测试；
   “OpenAI-compatible”不能自动解释为完整 Responses-compatible。
5. 迁移是否成功应以相同任务集上的正确率、工具成功率、completion gate、tokens、cache、延迟、成本和
   恢复行为为准，不以 endpoint 返回 200 为准。

## 7. 建议的验证矩阵

| 维度 | Chat Completions 基线 | Responses 候选 | 必查结果 |
| --- | --- | --- | --- |
| 单轮文本 | messages | input | 文本、usage、错误映射一致 |
| Structured Outputs | `response_format` | `text.format` | schema strictness 与拒绝路径 |
| 单/并行 function calls | tool calls in message | typed function call Items | `call_id`、参数流、结果关联 |
| 多轮 reasoning | 手工 message history | Items / `previous_response_id` | 正确率、reasoning tokens、cache、延迟 |
| Hosted tools | 自建 integration | Responses native tools | 引用、审批、费用、失败与超时 |
| 长上下文 | framework compaction | Responses compaction | canonical history、恢复、token 与质量 |
| 长任务 | 应用 job + request | background response | 取消、poll/webhook、超时、恢复 |
| 高频工具 loop | HTTP requests | Responses WebSocket | 断线、缓存缺失、重连、端到端延迟 |
| ZDR/stateless | 手工 history | encrypted reasoning Items | 数据策略、重放、错误恢复 |
| 第三方 provider | Chat-compatible | provider 声称的 Responses | schema 与能力契约，不只检查 HTTP 200 |

## 官方资料索引

- [Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Responses API Reference](https://platform.openai.com/docs/api-reference/responses)
- [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
- [Using tools](https://developers.openai.com/api/docs/guides/tools)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Agents SDK overview and Responses comparison](https://developers.openai.com/api/docs/guides/agents)
- [Background mode](https://developers.openai.com/api/docs/guides/background)
- [Compaction](https://developers.openai.com/api/docs/guides/compaction)
- [Responses WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode)
- [Realtime API](https://developers.openai.com/api/docs/guides/realtime)
- [Current model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [DeepSeek Responses API 兼容性明细](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)
- [Kimi Code provider 协议配置](https://www.kimi.com/code/docs/kimi-code-cli/configuration/providers.html#openai-responses)
