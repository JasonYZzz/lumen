# OpenAI Realtime API / GPT Realtime 官方技术调研

> 调研日期：2026-08-11  
> 范围：OpenAI 官方开发者文档、API Reference 与 OpenAI 官方 GitHub 文档。本文只记录平台事实、协议约束与明确标注的工程推断，不包含 Lumen 架构设计。

## 结论摘要

1. 截至调研日期，OpenAI 面向高质量实时语音 Agent 的当前主模型是 `gpt-realtime-2.1`；它支持音频输入/输出、文本输入/输出、图片输入、函数调用和可配置 reasoning effort，但不支持 Structured Outputs。模型上下文为 128,000 tokens，单次最大输出为 32,000 tokens。[GPT-Realtime-2.1 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
2. 浏览器和移动端应优先使用 WebRTC；服务端到服务端应优先使用 WebSocket。WebRTC 由媒体轨承载音频、RTCDataChannel 承载 Realtime 事件；WebSocket 则要求应用自行处理 Base64 音频、缓冲、播放进度和截断。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc) [WebSocket 指南](https://developers.openai.com/api/docs/guides/realtime-websocket)
3. 浏览器绝不能持有标准 OpenAI API key。官方提供两种 WebRTC 建连方式：后端代发 SDP 的 unified interface，以及由后端签发短期 client secret、浏览器直连 `/v1/realtime/calls` 的方式。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)
4. 浏览器可以直接与 OpenAI 建立 WebRTC，同时业务后端使用同一 call ID 建立 sideband WebSocket。该连接可以私下更新指令和工具、观察事件并执行工具调用，使敏感业务逻辑和工具凭据不进入浏览器。[Server controls 指南](https://developers.openai.com/api/docs/guides/realtime-server-controls)
5. Realtime 是有状态事件协议，但一次 Session 最长 60 分钟；voice 在首次音频输出后不可更换。官方文档没有给出传输断开后原地续接同一媒体连接的 resume token 协议。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)
6. Realtime 原生支持 VAD、语义 VAD、插话打断、输入转录和函数调用。WebRTC/SIP 下服务端能感知播放进度并自动截断未播放内容；WebSocket 客户端必须自行停止播放、计算已播放时长并发送 `conversation.item.truncate`。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations) [VAD 指南](https://developers.openai.com/api/docs/guides/realtime-vad)
7. OpenAI Agents SDK 已提供 Realtime 支持。TypeScript SDK 面向浏览器提供 `RealtimeAgent` / `RealtimeSession` 和 WebRTC transport；Python SDK 提供 Realtime runner/session/model，但默认是服务端 WebSocket，不负责浏览器 WebRTC。[Voice agents 指南](https://developers.openai.com/api/docs/guides/voice-agents) [Agents JS voice agents](https://openai.github.io/openai-agents-js/guides/voice-agents/) [Agents Python realtime guide](https://openai.github.io/openai-agents-python/realtime/guide/)

## 1. 当前模型与能力基线

官方 Realtime 总览当前以 `gpt-realtime-2.1` 作为低延迟语音 Agent 的起点。[Realtime API 总览](https://developers.openai.com/api/docs/guides/realtime)

`gpt-realtime-2.1` 的官方能力边界如下：

- 128,000-token context window，32,000-token maximum output。
- 输入支持 text、audio、image；输出支持 text、audio。
- 支持 function calling，不支持 Structured Outputs。
- 支持 speech-to-speech、工具使用、增强的指令遵循与可配置 reasoning effort。
- Rate limit 随 API usage tier 变化；Free tier 不支持。具体限额必须以账号和模型页实时显示为准。

来源：[GPT-Realtime-2.1 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)

官方同时提供成本更低的 `gpt-realtime-2.1-mini`。它同样支持 128k context、32k maximum output 和 function calling，但定位是蒸馏后的较小实时推理模型；是否满足复杂工具和指令场景需单独评估。[GPT-Realtime-2.1-mini 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)

## 2. 三类实时语音路径

| 路径 | 推荐运行位置 | 主要特点 | 官方定位 |
| --- | --- | --- | --- |
| Realtime + WebRTC | 浏览器、移动端 | 音频走媒体轨，事件走 DataChannel；原生适配实时媒体、抖动和打断 | 浏览器/移动端首选 |
| Realtime + WebSocket | 应用后端 | 单一双向事件流；应用自行编码、缓冲、播放和截断音频 | server-to-server 首选 |
| STT → text agent → TTS | 浏览器或后端编排 | 有显式文字中间态，易审核、控制和复用既有 text agent；相对增加延迟 | 可预测、结构化工作流 |

来源：[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc) [WebSocket 指南](https://developers.openai.com/api/docs/guides/realtime-websocket) [Voice agents 指南](https://developers.openai.com/api/docs/guides/voice-agents)

OpenAI 将 speech-to-speech 的优势概括为低延迟、自然轮次、可插话和实时工具调用；将 chained architecture 的优势概括为可预测性、已有文本 Agent 复用和中间状态可控。两者不是同一 API 的不同 transport，而是不同语音处理范式。[Voice agents 指南](https://developers.openai.com/api/docs/guides/voice-agents)

## 3. 浏览器 WebRTC 建连

### 3.1 共同的浏览器端流程

浏览器创建 `RTCPeerConnection`，把 `getUserMedia()` 获得的麦克风 track 加入 peer connection，并创建名为 `oai-events` 的 RTCDataChannel。音频通过 WebRTC media track 收发，Realtime client/server events 通过 DataChannel 发送和接收。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)

### 3.2 Unified interface：后端代发 SDP

官方给出的第一种方式是：

1. 浏览器创建 SDP offer，并把 offer 与 Session 配置发给应用后端。
2. 应用后端以标准 API key 调用 `POST https://api.openai.com/v1/realtime/calls`，以 multipart body 提交 `sdp` 与 `session`。
3. OpenAI 返回 SDP answer；应用后端把 answer 返回浏览器。
4. 浏览器调用 `setRemoteDescription()` 完成连接。

这种方式不需要单独签发 client secret，代码路径较短；代价是应用后端位于媒体 Session 初始化的关键路径。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)

### 3.3 Client secret：浏览器直连 calls endpoint

官方给出的第二种方式是：

1. 浏览器向自己的后端请求 Realtime client secret。
2. 后端用标准 API key 调用 `POST /v1/realtime/client_secrets`，并在请求中固定允许的 Session 配置。
3. 后端只把返回的短期 client secret 交给浏览器。
4. 浏览器带该 secret 将 SDP offer 直接 `POST /v1/realtime/calls`，接收 SDP answer。

标准 API key 只能留在受信任后端，不能打包或下发到浏览器。Client secret 应临近连接时签发，不应被当作长期凭据存储；有效期和返回字段应以实时 API 响应为准，本文不固化可能变化的 TTL 数值。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)

### 3.4 Sideband server connection

`/v1/realtime/calls` 的响应 `Location` header 包含 call ID。应用后端可用标准 API key 连接：

```text
wss://api.openai.com/v1/realtime?call_id=rtc_xxxxx
```

该 sideband WebSocket 与浏览器 WebRTC 共同操作同一个 Realtime Session。后端可以观察事件、更新 Session 指令、配置私有工具并处理工具调用；官方明确建议用它把工具逻辑和业务规则保留在服务端。[Server controls 指南](https://developers.openai.com/api/docs/guides/realtime-server-controls)

## 4. 服务端 WebSocket 建连

服务端以标准 API key 建立连接：

```text
wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1
```

双方交换 JSON 序列化的 Realtime events。应用必须自行采集、编码和 Base64 化输入音频，以 `input_audio_buffer.append` 分片发送；同样需要消费输出音频 delta、管理播放缓冲和中断。[WebSocket 指南](https://developers.openai.com/api/docs/guides/realtime-websocket)

官方说明浏览器技术上也能用 ephemeral token 建立 WebSocket，但对浏览器和移动端仍推荐 WebRTC，因为它对实时媒体更稳健。[WebSocket 指南](https://developers.openai.com/api/docs/guides/realtime-websocket)

## 5. Session、Conversation 与 Response

Realtime 的主要状态层次是：

- **Session**：模型、指令、工具、音频格式、voice、turn detection 等配置。
- **Conversation**：有序的 user、assistant、function call 和 function output items。
- **Response**：一次模型生成，可包含音频、转录文本、普通文本或 function call。

连接建立后服务端发送 `session.created`；客户端可发送 `session.update`，成功后收到 `session.updated`。Session 最长持续 60 分钟。大多数配置可在会话期间更新，但首次音频输出后不能再更换 voice。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

文本输入通过 `conversation.item.create` 插入 Conversation，再用 `response.create` 触发模型生成。输出应以 `response.done` 的实际 `status` 和内容作为该次生成的最终事实，而不能把收到任意事件或工具调用返回当作整体成功。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

Session 可以引用已存储 prompt，并由 Session request 中的 instructions/variables 覆盖或补充；这有利于复用稳定提示词，但动态值仍应由应用明确传入。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

## 6. 音频、输入转录、VAD 与插话

### 6.1 输入与输出音频

WebRTC 下，浏览器加入麦克风 track 后会自动发送输入音频，远端音频 track 可接入 `<audio>` 元素播放。WebSocket 下，应用需以 `input_audio_buffer.append` 发送 Base64 audio chunks；关闭 VAD 时还要显式 `commit` 并创建 Response。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

### 6.2 VAD 模式

Realtime speech-to-speech Session 默认启用 VAD，并提供两种 turn detection：

- `server_vad`：按声学静音切分，支持 `threshold`、`prefix_padding_ms`、`silence_duration_ms`，并可配置是否自动创建 Response、是否打断正在输出的 Response。
- `semantic_vad`：根据语义判断用户是否已经表达完毕，减少用户停顿时的过早抢话；`eagerness` 可为 `low`、`medium`、`high` 或 `auto`。

服务端以 `input_audio_buffer.speech_started` 和 `input_audio_buffer.speech_stopped` 通知语音边界。VAD 也可以关闭，改为 push-to-talk，由客户端显式 clear、append、commit 和 `response.create`；官方指出这能避免部分 VAD 误判。[VAD 指南](https://developers.openai.com/api/docs/guides/realtime-vad) [Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

### 6.3 插话与未播放音频截断

当 VAD 检测到用户开始说话时，可以取消当前 Response。关键问题不是只停止生成，而是让模型历史只保留用户真正听到的内容：

- WebRTC/SIP：OpenAI 服务器能感知客户端播放缓冲，自动截断未播放的 assistant audio。
- WebSocket：应用必须立即停止本地播放，计算该 assistant item 实际播放到的毫秒位置，并发送 `conversation.item.truncate`。

若 WebSocket 只停止扬声器却不截断 Conversation，模型后续会认为用户已经听完实际未播放的回答。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

### 6.4 输入转录

OpenAI 另有 `type: "transcription"` 的 Realtime transcription-only Session；它只转录音频，不生成语音助手回答。当前指南使用 `gpt-live-transcribe`，可通过 WebRTC 或 WebSocket 连接，支持 prompt、关键词、语言和 latency/accuracy delay 配置。[Realtime transcription 指南](https://developers.openai.com/api/docs/guides/realtime-transcription)

转录事件包含 delta 和 completed。跨多个语音 turn 的 completed events 不保证按音频顺序到达，应用应使用 `item_id` 关联和排序，而不是依赖到达次序。当前 `gpt-live-transcribe` 不提供逐词时间戳、说话人标签或 confidence。[Realtime transcription 指南](https://developers.openai.com/api/docs/guides/realtime-transcription)

在 voice-agent Session 中启用输入转录时，它是额外的模型路径和单独计费项；不应把异步转录完成事件等同于主 Realtime 模型的内部语义状态。[Realtime costs 指南](https://developers.openai.com/api/docs/guides/realtime-costs)

## 7. Function calling

工具可以配置在 Session 级或单次 Response 级。完整流程是：

1. 客户端通过 `session.update` 或 `response.create` 提供 function tool schema。
2. 模型生成 function call；参数可通过 `response.function_call_arguments.delta` 流式到达。
3. `response.done` 中可读取完整 function call、参数和 `call_id`。
4. 应用执行真实函数。
5. 应用发送 `conversation.item.create`，item 类型为 `function_call_output`，使用相同 `call_id`，`output` 是字符串。
6. 应用发送 `response.create` 让模型根据工具结果继续回答。

来源：[Realtime conversations 指南：Function calling](https://developers.openai.com/api/docs/guides/realtime-conversations)

工具调用由哪里执行取决于 Session 运行位置。Agents JS 官方文档特别提醒：如果 `RealtimeSession` 运行在浏览器，普通 function tool 代码也会在浏览器执行；敏感工具应放在服务端，或通过受控的服务端调用路径代理。[Agents JS voice agents build guide](https://openai.github.io/openai-agents-js/guides/voice-agents/build/)

## 8. 上下文、截断、缓存、用量与限额

一次 Voice Session 中，每次 Response 都会使用当前 Conversation，因此会话越长，后续输入 token 通常越多。官方给出的近似换算是：用户音频约每 100ms 一个 token，assistant 音频约每 50ms 一个 token，实际值可能略有差异。精确用量应读取 `response.done` 中的 usage。[Realtime costs 指南](https://developers.openai.com/api/docs/guides/realtime-costs)

Prompt caching 自动且为 best effort。保持 instructions、tool definitions 和早期 Conversation prefix 稳定可提高缓存命中；中途修改这些前缀可能使后续缓存失效。[Realtime costs 指南](https://developers.openai.com/api/docs/guides/realtime-costs)

达到可用输入上下文上限时，Realtime 默认从最旧 item 开始截断。可通过 retention-ratio 类配置一次保留较小比例以减少频繁小截断，也可禁用自动截断；禁用后，超限请求会返回错误。应用也可以主动删除旧 items 并写入摘要。[Realtime costs 指南](https://developers.openai.com/api/docs/guides/realtime-costs)

需要区分三个限制：

- 模型总 context window 和单次 maximum output：以模型页为准，`gpt-realtime-2.1` 当前为 128k / 32k。
- Session wall-clock duration：当前最长 60 分钟。
- 请求/token rate limit：随账号 usage tier 变化，不应写死为全局常量。

来源：[GPT-Realtime-2.1 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) [Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

## 9. 错误、取消与恢复边界

客户端事件应主动设置唯一 `event_id`。服务端 `error` event 会引用引发错误的 `event_id`，便于把异步错误对应到原始操作。官方 API Reference 说明大多数错误是可恢复的，Session 通常保持开放；应用仍应完整记录和处理 error events。[Realtime server events API Reference](https://platform.openai.com/docs/api-reference/realtime-server-events/conversation/item/input_audio_transcription/completed?lang=node)

在同一活动连接内，可通过取消 Response、清理输入 buffer、重发合法事件等方式继续；生成是否成功应检查 `response.done.status`。[Realtime conversations 指南](https://developers.openai.com/api/docs/guides/realtime-conversations)

**明确标注的工程推断：** 本次查阅的官方文档没有发现“传输断开后用 resume token 原地恢复既有 WebRTC/WebSocket 媒体流”的公开协议，也没有发现应用可假定未确认事件恰好执行一次的承诺。因此，连接丢失后更安全的解释是远端状态不确定：创建新连接/Session，只从应用自身已确认的 canonical conversation、工具 receipt 或摘要恢复，且不要自动重放结果未知的外部动作。该段是基于官方协议缺失做出的工程推断，不是 OpenAI 对恢复行为的官方保证。

## 10. 安全、凭据与数据治理

- 标准 OpenAI API key 只应位于受信任服务端；浏览器使用后端签发的短期 client secret，或让后端代理 SDP exchange。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- 创建 call 或 client secret 时，后端可设置 `OpenAI-Safety-Identifier`。官方建议使用稳定、隐私保护且不可直接识别用户的值，例如内部 user ID 的哈希；使用 client secret 时该标识会绑定到签发的 token。[WebRTC 指南](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- 私有 instructions、工具 schema、业务逻辑和工具凭据应留在 sideband server connection，不依赖浏览器保存秘密。[Server controls 指南](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- OpenAI API 数据默认不用于训练，除非组织主动 opt in。`/v1/realtime` 在官方数据控制表中标为无 application state retention、默认 abuse monitoring logs 最长保留 30 天，并可申请 Zero Data Retention；实际资格和组织设置必须在部署时核验。[Your data：endpoint policy table](https://developers.openai.com/api/docs/guides/your-data#default-usage-policies-by-endpoint)
- 官方数据驻留表列出 Realtime 支持 US/EU data residency，但相关 tracing 能力不一定与 `/v1/realtime` 具有相同驻留属性；启用 tracing 前应单独核查。[Your data 指南](https://developers.openai.com/api/docs/guides/your-data)

浏览器麦克风权限、录音提示、用户同意、音频存储期限和当地合规要求不由 Realtime transport 自动解决，属于部署应用必须另行实现和审查的责任。

## 11. OpenAI Agents SDK Realtime 能力

### 11.1 TypeScript

官方 TypeScript Agents SDK 提供 `RealtimeAgent` 与 `RealtimeSession`。浏览器中 `RealtimeSession.connect({ apiKey: ephemeralKey })` 默认通过 WebRTC 连接，并封装本地 history、音频传输、自动 interruption、tools、guardrails、handoffs 和审批等能力。[OpenAI voice agents 指南](https://developers.openai.com/api/docs/guides/voice-agents) [Agents JS voice agents](https://openai.github.io/openai-agents-js/guides/voice-agents/) [Agents JS transports](https://openai.github.io/openai-agents-js/guides/voice-agents/transport/)

SDK 是 Realtime API 上的客户端编排层，不改变底层安全原则：浏览器只拿 ephemeral key，敏感工具不能因 SDK 封装而被安全地下放到前端。[Agents JS voice agents build guide](https://openai.github.io/openai-agents-js/guides/voice-agents/build/)

### 11.2 Python

官方 Python Agents SDK 已有 Realtime layer，包括 `RealtimeAgent`、`RealtimeRunner`、`RealtimeSession` 和 `RealtimeModel`，并暴露音频、history、interruption、tool approval、error、usage 等事件。Python SDK 当前使用服务端 WebSocket transport，不提供浏览器 WebRTC transport；Web 前端仍需使用 Realtime WebRTC/API 或 TypeScript SDK。[Agents Python realtime guide](https://openai.github.io/openai-agents-python/realtime/guide/)

Python SDK 还提供面向 chained voice architecture 的 `VoicePipeline`；它与原生 speech-to-speech Realtime layer 是不同路径。[OpenAI voice agents 指南](https://developers.openai.com/api/docs/guides/voice-agents)

## 12. 官方协议事实清单

实施前至少应再次核对下列易变化事实：

- 部署账号实际可用的 Realtime model ID、rate limit 和价格。
- `/v1/realtime/client_secrets` 返回的有效期字段，而不是依赖历史 TTL 数值。
- 目标模型支持的 voice、audio format、turn detection 参数和区域可用性。
- 数据保留、ZDR 和 data residency 是否已经在目标组织获批。
- Agents SDK 的具体版本与其 transport 默认值。

以下内容在本次官方资料中**没有**得到可依赖的承诺：

- 断线后原 WebRTC/WebSocket 的通用 resume token。
- 网络边界上的 exactly-once event 或 function execution。
- 浏览器工具代码天然安全；实际相反，前端运行的工具必须按公开代码处理。
- 输入转录事件可作为主模型所见内容的逐字权威记录。

## 官方资料索引

- [Realtime API 总览](https://developers.openai.com/api/docs/guides/realtime)
- [GPT-Realtime-2.1 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
- [GPT-Realtime-2.1-mini 模型页](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)
- [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [Realtime API with WebSocket](https://developers.openai.com/api/docs/guides/realtime-websocket)
- [Realtime server controls / sideband](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad)
- [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)
- [Realtime costs](https://developers.openai.com/api/docs/guides/realtime-costs)
- [Voice agents](https://developers.openai.com/api/docs/guides/voice-agents)
- [Realtime API Reference](https://platform.openai.com/docs/api-reference/realtime?lang=javascript)
- [Your data](https://developers.openai.com/api/docs/guides/your-data)
- [OpenAI Agents SDK for TypeScript](https://openai.github.io/openai-agents-js/)
- [Agents JS voice agents](https://openai.github.io/openai-agents-js/guides/voice-agents/)
- [Agents JS voice transports](https://openai.github.io/openai-agents-js/guides/voice-agents/transport/)
- [Agents JS voice-agent build guide](https://openai.github.io/openai-agents-js/guides/voice-agents/build/)
- [OpenAI Agents SDK for Python: Realtime guide](https://openai.github.io/openai-agents-python/realtime/guide/)
