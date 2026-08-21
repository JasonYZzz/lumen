# Kimi K3 官方 API 配置核查

调研日期：2026-08-13  
范围：Kimi API 开放平台（中国站与国际站）、Kimi Code 官方文档、Kimi 官方 API Reference。  
证据边界：本次只核查公开的一手资料，未调用任何需要凭据的 endpoint，也未发起真实模型请求；
没有读取、记录或展示任何实际 API Key。账号是否已开通 K3、余额与区域归属，仍须用所属产品的
控制台或脱敏后的真实连通性测试确认。

## 结论

1. **Kimi API 开放平台的正式调用模型 ID 是 `kimi-k3`。** 官方 Model List、K3 Quickstart
   与 Chat Completions API Reference 均使用该 ID。不要把 Kimi Code 的 `k3` 写进开放平台配置。
   [Model List](https://platform.kimi.com/docs/models)
   [Kimi K3 Quickstart](https://platform.kimi.com/docs/guide/kimi-k3-quickstart)
   [Create Chat Completion](https://platform.kimi.com/docs/api/chat)
2. **中国大陆开放平台的 OpenAI-compatible `base_url` 是
   `https://api.moonshot.cn/v1`；国际开放平台是 `https://api.moonshot.ai/v1`。** 两个区域的
   账号和 Key 相互独立，Key 必须匹配申请它的平台。
   [中国站 API 概述](https://platform.kimi.com/docs/api/overview)
   [国际站 API Overview](https://platform.kimi.ai/docs/api/overview)
   [官方排障说明](https://www.kimi.com/help/kimi-api/api-troubleshooting)
3. **Kimi Code 是另一套独立产品。** 它的 OpenAI-compatible `base_url` 是
   `https://api.kimi.com/coding/v1`，K3 的模型 ID 是 `k3`；其 Key 来自 Kimi Code Console，
   不能与开放平台 Key 混用。官方也明确把 Kimi Code 定位为会员编程权益，把开放平台定位为
   按量计费的产品集成 API。
   [Kimi Code Overview：endpoint、模型 ID 与平台对比](https://www.kimi.com/code/docs/en/)
   [Kimi API 排障：产品 Key 不互通](https://www.kimi.com/help/kimi-api/api-troubleshooting)
4. **官方原生、明确文档化的推理协议是 OpenAI Chat Completions。** 开放平台 API 概述列出
   `POST /v1/chat/completions`，没有列出原生 `POST /v1/responses`；官方称其兼容的是 OpenAI
   Chat Completions 请求/响应格式。Kimi Code 官方 OpenAI-compatible endpoint 示例也为
   `/chat/completions`。Kimi 官方 Codex 集成文章在需要 Responses 协议时，专门使用本地 router
   把上游 Chat Completions 转换成 Responses-like API，这不是 Kimi 上游原生 Responses 支持。
   [中国站 API 概述](https://platform.kimi.com/docs/api/overview)
   [Kimi Code Overview](https://www.kimi.com/code/docs/en/)
   [Kimi 官方 Codex 集成说明](https://www.kimi.com/resources/codex-api)
5. **Kimi K3 开放平台上下文窗口为 1,048,576 tokens（1M）。**
   `max_completion_tokens` 默认 131,072，可设置的参数上限为 1,048,576；但输入与输出之和仍不得
   超过 1M，所以单次实际最大输出是 `1,048,576 - prompt_tokens`，还会受请求所设
   `max_completion_tokens` 限制。
   [Kimi K3 Quickstart](https://platform.kimi.com/docs/guide/kimi-k3-quickstart)
   [API troubleshooting：输出长度](https://www.kimi.com/help/kimi-api/api-troubleshooting)
6. **K3 支持 tool calling/function calling。** Chat Completions 接受 `tools`，返回
   `tool_calls`；K3 的 `tool_choice` 支持 `auto`、`none`、`required`。多轮工具调用必须把 API
   返回的完整 assistant message（包括 reasoning 内容）原样放回历史。
   [Tool Calls 指南](https://platform.kimi.com/docs/guide/use-kimi-api-to-complete-tool-calls)
   [Model Parameter Reference](https://platform.kimi.com/docs/api/models-overview)
7. **K3 永远开启推理。** 顶层 `reasoning_effort` 支持 `low`、`high`、`max`，默认 `max`；
   K3 的 `temperature` 固定为 `1.0`，官方建议不要显式传入该参数。
   [Model Parameter Reference](https://platform.kimi.com/docs/api/models-overview)

## 两套产品的准确配置映射

| 产品与凭据来源 | OpenAI-compatible `base_url` | K3 模型 ID | 上下文 | 原生已文档化协议 |
| --- | --- | --- | --- | --- |
| Kimi API 开放平台中国站 | `https://api.moonshot.cn/v1` | `kimi-k3` | 1M | Chat Completions |
| Kimi API 开放平台国际站 | `https://api.moonshot.ai/v1` | `kimi-k3` | 1M | Chat Completions |
| Kimi Code（会员编程权益） | `https://api.kimi.com/coding/v1` | `k3` | 会员档位决定，最高 1M | OpenAI-compatible Chat Completions；另支持 Anthropic Messages |

Kimi Code 还提供 `k3-256k`，它是 K3 的 256K 上下文版本；这并不是开放平台的模型 ID。
Kimi Code 官方文档说明，`k3` 的 1M 上下文是否可用取决于会员档位，而 `k3-256k` 固定为
256K。[Kimi Code Overview](https://www.kimi.com/code/docs/en/)

## Chat Completions 与 Responses 支持判断

截至本次核查，不能把“OpenAI-compatible”直接理解为同时兼容 OpenAI 的所有协议：

- 开放平台 API Overview 明确写的是 **OpenAI Chat Completions API** 兼容，并在 endpoint 列表中
  提供 `/v1/chat/completions`。
- Kimi K3 API Reference 也只把 K3 的消息、推理、视觉与工具调用定义在 Chat Completions contract
  下。
- Kimi 官方 Codex 指南说明，当前 Codex 自定义 provider 要求 Responses wire API，而只有 Chat
  Completions 的 Kimi 上游需要兼容层；其示例先请求 Moonshot 的 `/chat/completions`，再由本地
  router 暴露 `/v1/responses`。

因此，若客户端本身支持 Chat Completions，应直接使用对应 Kimi endpoint；若客户端硬性要求
Responses API，则需要明确的协议转换 Adapter，不能直接假定 Kimi endpoint 原生支持
`/v1/responses`。

## 配置与验证建议

1. 先根据 Key 的**签发控制台/产品**选定配置，不能依据字符串前缀猜测：
   - Kimi API 开放平台中国站签发：`.cn` + `kimi-k3`；
   - Kimi API 开放平台国际站签发：`.ai` + `kimi-k3`；
   - Kimi Code Console 签发：`api.kimi.com/coding/v1` + `k3`。
2. 不把真实 Key 写进仓库、测试 fixture、研究文档或日志；项目配置应引用环境变量。
3. 真实连通性验证至少检查：
   - 认证与模型可见性；
   - 一次最小非流式 Chat Completions；
   - 一次要求确定性函数调用的 tool-calling 回合；
   - 配置实际使用的 provider、base URL 与模型 ID，且日志已脱敏。
4. 开放平台官方说明 K3 需要充值后解锁；即使配置 schema 正确，账号未开通、区域不匹配或余额
   不足仍会导致调用失败。

## 官方来源

- [Kimi 中国站 API 概述](https://platform.kimi.com/docs/api/overview)
- [Kimi 国际站 API Overview](https://platform.kimi.ai/docs/api/overview)
- [Kimi Model List](https://platform.kimi.com/docs/models)
- [Kimi K3 Quickstart](https://platform.kimi.com/docs/guide/kimi-k3-quickstart)
- [Kimi Chat Completions API Reference](https://platform.kimi.com/docs/api/chat)
- [Kimi Model Parameter Reference](https://platform.kimi.com/docs/api/models-overview)
- [Kimi Tool Calls 指南](https://platform.kimi.com/docs/guide/use-kimi-api-to-complete-tool-calls)
- [Kimi API 官方排障说明](https://www.kimi.com/help/kimi-api/api-troubleshooting)
- [Kimi Code Overview](https://www.kimi.com/code/docs/en/)
- [Kimi 官方 Codex 集成说明](https://www.kimi.com/resources/codex-api)
