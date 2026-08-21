# DeepSeek-V4-Pro-0813 官方 API 模型核查

调研日期：2026-08-13  
范围：DeepSeek 官方 API 文档、官方模型与价格页、官方 API Reference、官方更新日志。  
证据边界：未调用需要账号凭据的实时 `/models` endpoint；模型可用 ID 以 DeepSeek 官方 API Reference
中的 `/models` 返回示例以及当日官方 Quick Start 为准。未把聚合平台、媒体、社区帖子或传言作为结论依据。

## 结论

1. **DeepSeek 当前最高规格的正式 API 路线是 V4-Pro；当前后端版本为
   `DeepSeek-V4-Pro-0813`。** DeepSeek 当日官方 Quick Start 明确写明：稳定模型名
   `deepseek-v4-pro` 已更新到 `DeepSeek-V4-Pro-0813`；官方模型与价格页也把
   `deepseek-v4-pro` 的 `MODEL VERSION` 列为 `DeepSeek-V4-Pro-0813`。
   [DeepSeek Quick Start](https://api-docs.deepseek.com/)
   [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)
2. **配置和调用时应使用 `deepseek-v4-pro`，不应写
   `deepseek-v4-pro-0813`。** `0813` 是当前被稳定别名指向的版本标识；DeepSeek 的官方调用示例继续使用
   `model="deepseek-v4-pro"`，并明确说明调用方式不变，使用该稳定名即可访问最新版本。
   [DeepSeek Quick Start](https://api-docs.deepseek.com/)
3. **官方目前确实存在、并正式提供可调用的 `deepseek-v4-pro`；没有把
   `deepseek-v4-pro-0813` 列为 API 模型 ID。** 官方 `/models` Reference 的返回示例只列出
   `deepseek-v4-flash` 与 `deepseek-v4-pro` 两个 ID。Chat Completions Reference 的 `model` 枚举也只接受这两个稳定 ID。
   [Lists Models](https://api-docs.deepseek.com/api/list-models)
   [Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)
4. 因而，针对 Lumen 的 OpenAI-compatible 配置，准确写法是
   `openai:deepseek-v4-pro`，并保留 DeepSeek 官方 `base_url`；不要把版本展示名拼成调用 ID。

## 官方证据逐项核对

| 核查项 | 官方事实 | 工程结论 |
| --- | --- | --- |
| 当前 V4-Pro 版本 | Quick Start 注释写明 `deepseek-v4-pro` 已更新为 `DeepSeek-V4-Pro-0813`；价格页的 `MODEL VERSION` 同样为 `DeepSeek-V4-Pro-0813` | `0813` 是截至 2026-08-13 的最新官方 Pro 后端版本 |
| OpenAI-compatible 模型 ID | Quick Start 调用示例使用 `deepseek-v4-pro`；`/models` 文档示例也返回该 ID | 配置 `openai:deepseek-v4-pro` |
| 日期后缀能否直接调用 | 官方 `/models` 示例和 Chat Completions 的模型枚举均未列出 `deepseek-v4-pro-0813` | 不把日期后缀作为模型 ID |
| 同期其他正式模型 | 官方页面同时列出 `deepseek-v4-flash`，其当前版本是 `DeepSeek-V4-Flash-0731` | V4-Flash 是更快、更经济的并列产品，不是 Pro 的更新替代品 |
| 是否仍是 4 月 Preview | 2026-04-24 的发布页明确把当时版本称为 V4 Preview；2026-07-31 更新日志又说明当时 Pro 正式版尚待发布。当前首页和价格页已把稳定别名更新到 `DeepSeek-V4-Pro-0813`，且当前版本字段不再标 Preview | 当前稳定别名已越过 4 月 Preview 版本；配置应跟随别名，无需固定日期后缀 |

来源：

- [DeepSeek Quick Start：当前模型名、当前映射版本与官方调用示例](https://api-docs.deepseek.com/)
- [Models & Pricing：当前模型版本、上下文、功能与定价](https://api-docs.deepseek.com/quick_start/pricing/)
- [Lists Models：官方 `/models` API schema 与返回示例](https://api-docs.deepseek.com/api/list-models)
- [Create Chat Completion：官方 Chat Completions 模型枚举](https://api-docs.deepseek.com/api/create-chat-completion)
- [Change Log：2026-07-31 Flash 正式版更新及当时 Pro 状态](https://api-docs.deepseek.com/updates/)
- [DeepSeek V4 Preview Release：2026-04-24 Preview 发布与最初 API 稳定别名](https://api-docs.deepseek.com/news/news260424/)

## “正式版”措辞说明

DeepSeek 当前首页和模型价格页已经把 `DeepSeek-V4-Pro-0813` 作为
`deepseek-v4-pro` 的现行版本提供，但截至本次核查，官方更新日志的最新条目仍是 2026-07-31 的
V4-Flash 更新，新闻目录也尚未出现单独的 V4-Pro-0813 发布文章。因此可以准确断言的是：

- `DeepSeek-V4-Pro-0813` 是 DeepSeek 官方当前部署的 Pro 版本；
- 它可通过正式、稳定 API 模型名 `deepseek-v4-pro` 调用；
- `deepseek-v4-pro-0813` 不是官方文档列出的调用 ID。

若把“正式版”严格限定为“必须有独立发布公告”，现有官方材料尚缺该公告；若按官方当前生产
模型目录、价格页与调用文档判断，则 `0813` 已是稳定别名背后的现行正式 Pro 版本。
