# Lumen 网页检索与文件下载能力审计

日期：2026-09-10。

## 结论

截图中的 `https://api.jikan.moe/v4/manga/136488` 返回 504，是可重试的上游临时故障；更关键的产品问题是模型把“读取 API 响应”路由到了会创建 Work Product 的 `download_file`。Lumen 并非没有 URL 读取能力，但原有工具描述没有清楚区分“阅读”和“保存”，HTTP Adapter 又缺少请求标识、瞬时错误重试与真正的流式读取上限，因此用户感知接近“基本都会失败”。

本轮保持三层能力而不制造第二套网络权威：provider 原生 `web_search` 负责支持它的模型；配置型 Tavily/Brave 或 MCP 负责搜索发现；内置 `web_fetch` 负责读取已知公共 URL 的静态 HTML、API、JSON、RSS/XML。`download_file` 只负责把已知原始 UTF-8 资源原样、原子地写入工作区。

## Codex、Claude Code 与 Pi 的公开实现对照

| Harness | 公开能力 | 对 Lumen 的启示 |
|---|---|---|
| Codex | Responses API 可把 hosted `web_search` 作为模型工具；Codex 的 `web.run` 还统一暴露 search、open、click、find 与 PDF screenshot 等动作。 | 搜索、页面读取与浏览动作应是明确语义，不应让“下载文件”承担阅读。provider 原生搜索适合有来源的开放式检索，本地 HTTP 抓取仍用于确定 URL。 |
| Claude Code | 内建 `WebSearch` 与 `WebFetch`。搜索返回标题/URL，再由 Fetch 读取；Fetch 对内容运行提取 prompt、截断大页面、缓存、限制跨域重定向并对提取过载做退避重试。 | 搜索发现和 URL 阅读要分开；工具描述、域权限、截断与重试共同决定可靠性。Lumen 不复制 Claude 的服务端提取层，但保留链接，方便模型继续访问来源。 |
| Pi | 默认只有 `read`、`write`、`edit`、`bash`，网页能力不是 core 内建；用户通过 bash 或 extension/package 注册自定义工具。 | Pi 的成功来自可扩展性，不代表 curl 是稳定网页 Interface。Lumen 已有受 SSRF、审批和响应上限约束的专用 Interface，应继续深化它，而不是退回 shell 网络。 |

公开依据：

- [OpenAI Web search guide](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Codex `web.run` tool description](https://github.com/openai/codex/blob/main/codex-rs/ext/web-search/web_run_description.md)
- [Claude Code tools reference](https://code.claude.com/docs/en/tools-reference)
- [Pi coding-agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
- [Pi extensions documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)

## 已落地的优化

1. `web_fetch` 的描述明确覆盖网页、API、JSON 与 RSS/XML，并声明不写工作区；`download_file` 明确禁止用于阅读任务。
2. 两个 HTTP 工具发送稳定的 Accept 与 User-Agent，减少服务端把默认 `python-httpx` 客户端当作未知调用方拒绝的概率。
3. 对 408、425、429、500、502、503、504 和传输故障做最多三次指数退避重试；数值型 `Retry-After` 优先，单次等待封顶 10 秒。
4. `web_fetch` 从“完整读入再切片”改为流式停止，`fetch_max_bytes` 现在是真正的响应正文上限。
5. HTML 转文本时保留绝对链接，避免搜索/研究链路丢失来源入口。
6. 每次重定向和每次重试仍重新执行公共主机校验；`download_file` 在最终成功前不发布文件，原子写入、哈希、UTF-8、HTML/二进制拒绝与 TaskWorkspace journal 均保留。

## 明确未伪装支持的范围

内置 `web_fetch` 是 HTTP 文本读取器，不是浏览器。它不执行 JavaScript、不登录、不点击，也不绕过 Cloudflare 等挑战。此类需求应通过受控 Browser MCP 或 provider 原生联网能力完成；把浏览器运行时直接塞进 `download_file` 会混淆 Risk、EffectKind、会话状态和凭据边界。
