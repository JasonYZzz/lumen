# 国产实时语音 Provider 调研与 Lumen Router 建议

更新时间：2026-08-11

## 结论

Lumen 的 Web Live 可以进行国产替换，但这是 Provider Adapter 与媒体链路级替换，不能只修改
`model`、`base_url` 或兼容 OpenAI 的 HTTP Client。

- 首选百炼 `qwen3.5-omni-plus-realtime`，低成本 Route 使用
  `qwen3.5-omni-flash-realtime`。它们具有 WebSocket、WebRTC、语义 VAD、打断、转写和
  Function Calling，是当前协议形态最接近 OpenAI Realtime 的国产模型。
- `qwen-audio-3.0-realtime-plus/flash` 是百炼面向纯语音对话的另一组候选，适合后续
  server WebSocket Route；当前文档和 WebRTC 示例成熟度不如 Omni。
- 火山引擎具备豆包端到端实时语音模型，也有完整的 RTC AI 音视频互动方案；它更像
  “媒体网络 + 托管编排”产品，不是 OpenAI Realtime wire-compatible 模型 endpoint。
  适合作为第二阶段的独立 RTC Adapter，而不是第一阶段默认实现。

## Lumen 当前实现的约束

当前 `RealtimeTransport` 虽名为通用 Interface，实际固定了“浏览器 SDP + Provider call ID +
服务端 sideband”拓扑；`LiveSessionManager` 直接解析和发送 OpenAI Realtime raw events；Web
客户端也固定创建 `oai-events` DataChannel 并发送 `response.cancel`。`LiveConfig.provider` 只允许
`openai`，ResourceManager 直接实例化 `OpenAIRealtimeAdapter`。

因此在现状上增加 `if provider == "bailian"` 只会把 OpenAI 协议假设散落到更多调用点，不能形成
可替换的深 Module。

## 官方能力对照

| 维度 | 百炼 Qwen3.5 Omni Realtime | 百炼 Qwen-Audio 3.0 Realtime | 火山豆包实时语音 / RTC AI |
|---|---|---|---|
| 实时语音 | 端到端文本/音频/图像输入，文本/音频输出 | 专注音频/文本实时对话 | 端到端 S2S，或 ASR/LLM/TTS 级联/混合编排 |
| 浏览器媒体 | WebRTC；也支持 WebSocket、AOQ | 官方总览以 WebSocket 为主 | 火山 RTC Web SDK + 房间；另有专有 WebSocket |
| 打断/VAD | server VAD、semantic VAD、`response.cancel` | server VAD、smart turn、自动取消 | VAD、判停、UpdateVoiceChat 打断 |
| 转写 | 输入/输出 transcript events | 输入/输出 transcript events | RTC 字幕/任务事件 |
| Function Calling | 支持，模型自主决定 | 支持，模型自主决定 | RTC AI 支持 FC、并行 FC、MCP；混合模式可由 LLM 处理 FC |
| 服务端控制 | 原生 WebSocket 支持；公开 WebRTC 文档未发现 OpenAI 式 sideband | WebSocket | Start/Update/StopVoiceChat 控制面和任务事件 |
| 协议兼容性 | 事件名相近，但未声明 OpenAI wire compatibility | 不应假设 OpenAI 兼容 | 未声明 OpenAI Realtime Agent 协议兼容 |
| 区域 | 北京、新加坡，Key 按地域隔离 | 北京、新加坡 | RTC 网络覆盖广；豆包语音 SLA/数据区域需单独核实 |

百炼官方文档显示：

- `qwen3.5-omni-plus-realtime`、`qwen3.5-omni-flash-realtime` 支持 WebSocket、WebRTC 和 AOQ。
- WebRTC 通过 SDP HTTP endpoint 建连，音频走 RTP，事件走 DataChannel；官方建议 AppServer
  代理 SDP，避免向浏览器暴露永久 API Key。
- Function Calling 使用 `response.function_call_arguments.done`、
  `conversation.item.create(function_call_output)` 和 `response.create` 等相似事件。
- `tools` 与模型内置 Web Search 互斥。
- Omni WebSocket 单连接最长 120 分钟；Plus/Flash 分别保留最多 100/80 个音频轮次以及
  600/480 秒音频上下文。上下文音频时长不等于连接时长。

百炼当前公开 `session.update` 文档只描述模型“自主决定”是否调用工具，未找到
`tool_choice=required` 的等价保证。它也未公开按 WebRTC call ID 建立服务端 sideband 的 Interface。
这两点会影响 Lumen 当前强制 `complete_live_turn` 和服务端工具权威的实现。

火山官方资料显示：

- 豆包端到端实时语音使用 Speech-to-Speech，支持低延迟和自然打断。
- RTC AI 音视频互动通过 `StartVoiceChat`、`UpdateVoiceChat`、`StopVoiceChat` 管理任务，支持
  字幕、VAD、打断、Function Calling、并行函数调用和 MCP。
- 端到端模型在 RTC 配置中是 `S2SConfig`（例如 Provider 为 `volcano`、模型类型为 `O`），
  不是可直接填入现有 OpenAI `model` 字段的 Ark 模型 ID。
- 混合输出模式适合“普通对话走 S2S，工具意图交给 LLM/Lumen”；工具结果可携带
  ToolCallID 通过 UpdateVoiceChat 回填。
- 火山硬件智能体 WebSocket 需要设备注册和签名，不适合浏览器直接使用。

## 安全结论

百炼 direct WebRTC 的 Function Calling 事件公开路径在浏览器 DataChannel。将这些 raw events
从浏览器转发给 Host 虽可快速实现，但浏览器不能向 Host 证明事件确实来自 Provider；XSS 或被篡改
客户端可以伪造工具调用。因此该拓扑只能用于明确的 voice-only/read-only Profile，不能成为具有
本地文件、命令、MCP 或外部动作能力的 Lumen Agent 默认路径。

默认 full-agent Route 应采用服务端控制：浏览器只承载音频，Lumen Host 直接连接 Provider
WebSocket，规范化工具事件并通过现有 `CapabilityGateway`、审批、EffectReceipt 和 completion gate
执行。Provider key、火山 AK/SK、AppKey 和语音 Token 均不得进入浏览器。

## 推荐架构

### 1. Canonical realtime protocol

新增 Provider-neutral 的值对象：

- `LiveSessionSpec`：instructions、tools、voice、turn detection、transcription、completion policy。
- `LiveProviderCapabilities`：media modes、barge-in、transcription、function calling、
  server tool authority、completion-control、session limits。
- `LiveProviderEvent`：speech/input transcript/response transcript/audio/tool call/usage/error。
- `LiveProviderCommand`：cancel、tool result、continue、approved answer、close。

`LiveSessionManager` 只处理 canonical event/command；Provider raw JSON 的编解码完全留在 Adapter。

### 2. Provider Adapter 与 Media Adapter 分离

- `OpenAIRealtimeAdapter`：保留 direct WebRTC + server sideband Implementation。
- `BailianRealtimeAdapter`：首版采用 Lumen 服务端到百炼的原生 WebSocket。
- `VolcRtcRealtimeAdapter`：服务端 Start/Update/StopVoiceChat 和 task event/FC 回填。
- Web 端 `LiveMediaClient` 按握手类型选择现有 direct WebRTC、Lumen PCM WebSocket 或火山 RTC SDK。

启动响应应由固定的 `answerSdp` 升级为 discriminated handshake，例如 direct WebRTC、PCM WebSocket
或 managed RTC；UI 状态和 Host SSE Interface 保持不变。

### 3. LiveProviderRouter

Router 只负责选择一个已验证的 Route，不负责理解厂商 raw event。Route 是创建时冻结的配置快照：

```text
LiveRoute
├── provider / model / region
├── provider_adapter
├── media_adapter
├── capabilities
├── completion_mode
├── session_limits
└── admission / health
```

按 required capabilities 选择 Route；缺少 server tool authority、strict completion 或 transcription
时安全失败。只允许在媒体建立和副作用发生前按有序 Profile fallback；不得在通话中途、工具调用后或
外部动作后静默切换模型。

### 4. Completion gate

定义三种明确能力，而不是假设每家支持 OpenAI `tool_choice=required`：

- `native_required_tool`：Provider 原生强制完成工具。
- `host_gated_synthesis`：实时模型先输出文本，Host 验证后再以流式 TTS 播放。
- `advisory_only`：只适用于无副作用的普通语音聊天，不能声称满足 strict completion。

百炼 full-agent 默认应使用 `host_gated_synthesis`，或者在 Provider 实测确认具备等价强制工具协议后
再启用 native 模式；不得静默把 strict 降级为 advisory。

## 推荐实施顺序

1. 抽取 canonical protocol、capability contract 和 Provider contract tests。
2. 实现 LiveProviderRouter、严格的 discriminated provider config 和 route snapshot。
3. 将 OpenAI 现实现迁移到新 Adapter，保证行为不变。
4. 实现百炼 server WebSocket Adapter 与浏览器 PCM AudioWorklet/WebSocket Media Adapter。
5. 实现 strict completion 的 host-gated text-to-speech 路径。
6. 以真实北京百炼 Workspace 做 admission、延迟、打断、工具调用和长会话 smoke test。
7. 第二阶段实现火山 RTC Adapter；禁用其内置 MCP/记忆/RAG，避免与 Lumen 状态权威重叠。

## 官方来源

- [百炼 Realtime API 总览](https://help.aliyun.com/zh/model-studio/realtime-api-overview)
- [Qwen Omni Realtime](https://help.aliyun.com/zh/model-studio/realtime)
- [百炼 Client Events](https://help.aliyun.com/en/model-studio/client-events)
- [百炼 Server Events](https://help.aliyun.com/en/model-studio/server-events)
- [Qwen3.5 Omni WebRTC 最佳实践](https://help.aliyun.com/zh/model-studio/best-practice-webrtc-omni-realtime)
- [百炼 Function Calling](https://help.aliyun.com/zh/model-studio/qwen-function-calling)
- [百炼语音转语音模型选型](https://help.aliyun.com/zh/model-studio/s2s-model)
- [火山 RTC AI 音视频互动文档目录](https://www.volcengine.com/docs/6348/?lang=zh)
- [火山 RTC AI 发版说明](https://www.volcengine.com/docs/6348/1544162?lang=zh)
- [火山 StartVoiceChat API](https://api.volcengine.com/api-docs/view?action=StartVoiceChat&serviceCode=rtc&version=2024-06-01)
- [火山端到端实时语音模型](https://www.volcengine.com/docs/6561/1594360?lang=zh)
- [火山豆包实时语音产品页](https://www.volcengine.com/product/realtime-voice-model)
- [火山 RTC AI 官方 Demo](https://github.com/volcengine/rtc-aigc-demo)

## 未确认项

- 百炼 WebRTC 准入、白名单和实际 endpoint 应以目标 Workspace 的控制台和联调结果为准。
- 百炼未公开 OpenAI 式 server sideband；后续若新增官方 Interface，再评估 direct WebRTC full-agent。
- 火山各版本 StartVoiceChat 字段和 FC 事件投递方式应在选定 API 版本后做真实账号联调。
- 两家并发、价格、数据驻留和商用 SLA 需要结合实际账号、地域及合同确认，不能由模型文档推断。
