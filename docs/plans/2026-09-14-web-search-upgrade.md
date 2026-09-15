# Web Search / Fetch 工具升级实施计划

日期：2026-09-14（v2，纳入结构化输出升级与第二轮深度调研结论）。状态：Phase 0/1/2 已实现（SearXNG + Trafilatura + 结构化输出 + Crawl4AI 浏览器层）；Phase 3 仅凭证据启动。

调研输入：

- 用户调研（2026-09）：SearXNG + Crawl4AI + Scrapling + Playwright + Trafilatura 零成本自部署方案。
- 第二轮深度调研（2026-09-14）：SearXNG JSON API 事实核查、Crawl4AI 0.9.x API 与安全边界核查、Claude Code / OpenAI / Codex / Tavily / Brave / MCP 的结构化输出约定对比、代码内 `ToolOutputSpec` 机制盘点。关键结论见附录 A。

## 1. 目标

把 Lumen 默认的 Web 能力从「Tavily/Brave 付费 API + stdlib HTML 剥标签 + 纯文本结果」升级为：

1. **搜索**：支持自托管 SearXNG 作为免费默认 Search Provider，保留 Tavily/Brave 作为可选商业 Provider。
2. **读取**：`web_fetch` 从「纯 HTTP + 手写 HTMLParser」升级为分层策略链——Trafilatura 正文抽取（快速路径）→ Crawl4AI 浏览器渲染（JS 页面）→ 结构化 Markdown 输出。
3. **结构化输出**：`web_search` / `web_fetch` 成为仓库内首批声明 `ToolOutputSpec` 的生产工具，结果用 Pydantic 模型表达，替代纯文本拼接。
4. **不破坏既有边界**：SSRF 防护、Risk/EffectKind 声明、审批语义、分页截断契约全部保留；不引入第二套网络权威。

明确非目标（本期不做）：

- Scrapling Stealth、Playwright 自定义浏览器自动化（登录/点击/滚动）——仅作为 Phase 3 候选，需先以真实失败样本证明 Crawl4AI 不够。
- 住宅代理 / 验证码打码等付费反爬基础设施。
- 修改 provider 原生 `web_search`（模型侧联网）层——它是独立的第三层，本次不动。
- 多查询批量搜索（Codex `web.run` 风格）、引用偏移标注（OpenAI annotation 风格）——调研确认这些属于 Host 渲染层或另一工具语义，不塞进本工具的输出模型。

## 2. 现状盘点（升级前的事实基线）

### 2.1 搜索

- 唯一实现：`src/lumen/tools/web.py:482-540` `build_web_search_spec()`。无 Provider Interface，Tavily/Brave 是闭包内 if/else 字符串分支（`web.py:503-526`）。
- 返回类型是拼接好的纯文本 `str`（`_format_result`，`web.py:475-479`），无 Pydantic 结果模型，无 `ToolOutputSpec`。
- 配置：`WebSearchConfig`（`src/lumen/config.py:311-321`）只有 `provider: Literal["tavily","brave"]`、`api_key_env`、`max_results`。
- 注册：`src/lumen/resources.py:287-294`，仅当 `tools.web.search` 非空时注册；不在 `builtins` 默认列表。
- **测试盲区**：Tavily/Brave 搜索闭包零测试覆盖。

### 2.2 读取

- `build_web_fetch_spec()`（`web.py:255-321`）：sync `httpx.Client`、流式截断（`fetch_max_bytes` 默认 2 MiB）、408/425/429/5xx 指数退避重试、逐跳重定向 SSRF 校验（`is_public_host` / `validate_public_url`，`web.py:104-134`）。
- HTML 清洗是手写 `_TextExtractor(HTMLParser)`（`web.py:63-101`）：无正文识别、无 Markdown 输出，仅剥 script/style 并保留绝对链接。
- 返回 dict 分页契约（`content/has_more/next_start_char/total_chars` 等，`web.py:299-309`），模型已依赖该形状，**字段必须保持**（升级为 Pydantic 模型时原样落为字段）。
- 测试：`tests/test_download_file.py`（含 web_fetch 重试/字节上限用例），走 `httpx.MockTransport` + `host_guard` 注入 seam。

### 2.3 结构化输出机制（第二轮代码盘点新增）

- `ToolOutputSpec`（`src/lumen/tools/spec.py:67-105`）机制已完整存在：**strict 校验 → canonical JSON-mode dict → `ToolReturnPart.content`**；`model_text` 渲染只用于事件/Hook/错误路径。但目前**没有任何生产工具声明它**——我们是第一个，需补契约测试锁定行为。
- 关键约束：模型看到的是 canonical dict 本身，`render_model` **不能**用来控制模型可见体积——截断必须留在工具内部（`web_fetch` 分页模式已是先例）。
- 校验失败是类型化失败：gateway 转为 `CapabilityResult(status=FAILED)`，runtime 转 `RetryPromptPart`（`gateway.py:602-613`、`runtime.py:1632-1643`），不会崩溃。
- Session journal 已存 dict 型工具返回（`web_fetch`、`git_*` 都是），**无 schema 迁移**；但 `web_search` 从 str 变 dict 会带来 snapshot/测试 churn。
- 待核查点：`src/lumen/context/transcript.py:51-61` 的 receipt 化对非 str content 走 `str(content)`（repr 式转储），结构化输出上线前必须确认大 dict 的 receipt 渲染不退化。
- 兼容性确认：`runtime.py:1909-1918` 从 Mapping 型 canonical output 提取 `exit_code`，dict 模型与此路径兼容。
- Prompt 层：`build_web_guidance`（`instructions.py:98-133`）只描述路由、不描述输出形状，无需改；**需要改的是工具 docstring 与 `ToolSpec.description`**（它们进入 pydantic-ai 参数 schema）。

### 2.4 横切约束

- 两个工具均 `Risk.EXTERNAL` + `EffectKind.OBSERVE`；auto 模式自动批准，manual 模式逐次询问；Plan 模式只信 `Risk.READ`，**web 工具在 Plan 模式被拦**（`src/lumen/approval.py:78-87`）——本期维持现状，但在工具描述与文档中写清。
- `httpx` 目前只是 `live` extra 里的可选依赖，靠 `pydantic-ai-slim`/mcp 传递到达（`pyproject.toml:27-30`）。本次升级把 HTTP 链路变成主路径，**必须把 `httpx` 提升为核心依赖**。
- 2026-09-10 审计（`docs/research/2026-09-10-web-retrieval-capability-audit.md`）曾明确「浏览器运行时不进内置工具，浏览器需求走受控 Browser MCP」。本计划是对该结论的正式修订：当时的前提是「内置 fetch 只做 HTTP 文本读取」；现在目标变为「默认工具要能吃 JS 渲染页面」，且 Crawl4AI 提供了可自部署、带明确失败语义（`success=False` + 反爬检测）的成熟实现。修订理由与范围需在 `docs/architecture-guide/04-tools-permissions-security.md` 同步记录。

## 3. 目标设计

### 3.1 分层与权威

保持单一网络权威 `lumen.tools.web` 包，内部按策略拆分 Module，调用方（`resources.py`）只面对同样的 `build_*_spec()` 工厂：

```
src/lumen/tools/web/
├── __init__.py          # 对外仍只暴露 build_web_fetch_spec / build_web_search_spec / build_download_file_spec
├── http.py              # 现有 httpx 传输、重试、SSRF、有界流式读取（从 web.py 原样迁移）
├── extract.py           # _TextExtractor 迁移 + Trafilatura 快速路径
├── models.py            # SearchResultItem / WebSearchResult / WebFetchResult（新增，见 3.4）
├── search/
│   ├── __init__.py      # SearchProvider Protocol + build_web_search_spec（按 config 选 Provider）
│   ├── brave.py         # 现有 Brave 分支搬迁 + 结构化映射
│   ├── tavily.py        # 现有 Tavily 分支搬迁 + 结构化映射
│   └── searxng.py       # 新增
└── browser.py           # Phase 2：Crawl4AI 渲染策略（可选依赖，惰性 import）
```

不新增公开 Interface 给外部实现——`SearchProvider` 是模块内部 Protocol，只有确实存在两个以上实现（Brave/Tavily/SearXNG）才抽象，符合 deletion test。

### 3.2 web_fetch 策略链

```
URL
 │
 ├─ ① FastFetch：httpx 流式 GET（现有路径，含 SSRF/重试/字节上限）
 │     └─ HTML → Trafilatura extract(markdown) → 命中正文且 ≥ min_chars？── 是 → 分页返回
 │        否（JS 空壳、正文过短、抽取失败）↓
 │
 ├─ ② BrowserFetch（Phase 2，可选）：Crawl4AI AsyncWebCrawler
 │     └─ 渲染 → markdown.fit_markdown or raw_markdown → 成功且非空？── 是 → 分页返回
 │        success=False / 未安装 crawl4ai ↓
 │
 └─ ③ 回退现有 _TextExtractor 剥标签结果（今天的兜底行为，永不更差）
```

关键决策：

- **判定标准要可观测**：降级原因（`js_shell` / `thin_content` / `extract_failed` / `blocked`）落在 `WebFetchResult.fetch_strategy` / `fallback_reason` 字段上，供测试与排查。
- **Crawl4AI 失败语义明确**：其对 Cloudflare/Akamai 挑战页、403/429 薄响应体会返回 `success=False` 并给出 `error_message`，不静默返回空壳——直接映射为降级到 ③，不做无尽重试。
- **异步**：Crawl4AI 是 async API。`web_fetch` 闭包改为 async（`download_file` 已是 async，ToolSpec 支持）。
- **浏览器生命周期**：`AsyncWebCrawler` 进程级懒加载单例，手动 `start()/close()`（官方对长驻应用的推荐模式），随 Host 关闭释放；不为每次 fetch 启停 Chromium。内存泄漏在上游是反复出现的历史问题（0.9.x 修了一批），单例 + 关闭钩子是本期范围，周期性重建只记录为已知 hedge，出问题再加。
- **分页契约不变**：无论哪条路径产出内容，都走同一个分页切片函数，`content/has_more/next_start_char/total_chars` 形状冻结，加契约测试锁定。

### 3.3 SearXNG Search Provider

基于第二轮调研的事实修正（附录 A.1），客户端实现要点：

- 请求：`GET {base_url}/search?q=...&format=json`，可选参数 `engines`、`language`、`safesearch`、`time_range` 从配置透传。
- **忽略 `number_of_results`**（上游已知不可靠/已移除），以 `len(results)` 为准。
- `unresponsive_engines` 映射为结果模型的诊断字段，不让模型把「引擎被挂起」误读为「没有结果」。
- 超时：客户端 10–15s（SearXNG 引擎扇出等最慢者，默认引擎超时 3s），独立于 `fetch_timeout_seconds`。
- **SSRF 例外**：SearXNG 通常部署在 localhost/内网，`is_public_host` 会拒绝。处理方式是「显式配置即信任」：`base_url` 来自用户配置文件，视为操作者明确信任的目标，跳过公共主机校验；在配置校验和文档中写清。**不**为此放开全局 SSRF 开关。
- 无 API Key（SearXNG 核心无内建认证）；`api_key_env` 对 searxng 保留为可选（用于反代 Basic Auth/Bearer 场景）。
- 部署责任在操作者：文档给出 checklist（`search.formats` 加 `json` 否则 403、`limiter`/`public_instance` 保持关闭、绑定 localhost、推荐引擎 duckduckgo/brave/bing/baidu、Google 仅 best-effort），框架内不做健康检查。

### 3.4 结构化输出模型（本期新增核心交付）

调研结论（附录 A.3）：Tavily ∩ Brave ∩ Claude Code 的交集是 `title/url/snippet/published/score`；截断必须对模型可读；引用标注属于 Host 渲染层不进工具结果。据此定义 `tools/web/models.py`：

```python
class SearchResultItem(BaseModel):
    model_config = ConfigDict(extra="ignore")   # 面向模型的模型，刻意不用 forbid：provider 加字段不破坏反序列化

    title: str = ""
    url: str
    snippet: str = ""
    published: str | None = None                # 在 provider seam 归一化为 ISO 8601（Tavily 是 RFC-2822、Brave 是 ISO）
    score: float | None = Field(default=None, ge=0, le=1)

class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str
    provider: str                               # 溯源/调试
    answer: str | None = None                   # Tavily 综合答案；Brave/SearXNG 无
    results: list[SearchResultItem]
    engine_errors: list[str] = []               # SearXNG unresponsive_engines 等诊断

class WebFetchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str                                    # 最终 URL
    content_type: str
    title: str | None = None                    # Trafilatura Document 元数据（FastFetch 路径）
    author: str | None = None
    date: str | None = None
    sitename: str | None = None
    content: str                                # markdown / 文本分页切片
    # —— 以下为现有分页契约字段，形状冻结 ——
    start_char: int
    end_char: int
    has_more: bool
    next_start_char: int | None
    total_chars: int
    truncated: bool
    # —— 新增可观测字段 ——
    fetch_strategy: str                         # "fast" | "browser" | "text_fallback"
    fallback_reason: str | None = None
```

接入方式与约束：

- 两个 `ToolSpec` 声明 `output=ToolOutputSpec(WebSearchResult/WebFetchResult)`；canonical dict 即模型所见（`runtime.py:1591-1631` 已支持）。
- `extra="ignore"` 是对仓库「持久化模型用 forbid」惯例的**刻意偏离**：这两个模型不是持久化记录，是 provider 响应的前向兼容映射；在 models.py 顶部注释写明理由。
- 大正文截断仍由工具内部分页完成，不指望 `render_model`。
- 工具 docstring 与 `ToolSpec.description` 同步改写为描述新字段（替换「title — url — snippet 行」的旧描述）。
- SearXNG/Tavily/Brave 各自在 Provider 模块内完成 wire 字段 → `SearchResultItem` 的映射（含日期归一化）；Brave 字段映射在冻结前用一次真实响应核对（调研未能访问 Brave 官方文档页）。

### 3.5 配置演进（schema v2 内加性变更，不升版本）

```yaml
tools:
  web:
    fetch_timeout_seconds: 20.0
    fetch_max_bytes: 2097152
    fetch_strategy: auto          # 新增：auto(默认链) | http_only（禁用浏览器路径）
    search:
      provider: searxng           # Literal 扩为 "tavily" | "brave" | "searxng"
      base_url: http://127.0.0.1:8080   # searxng 必填，其余 provider 禁止（validator）
      api_key_env: SEARXNG_TOKEN  # 仅 searxng 可选（反代认证场景）
      max_results: 8
      engines: [duckduckgo, brave, bing, baidu]   # 可选；pin 住已知可用引擎，避免坏引擎拖垮查询
      language: all               # 可选透传
```

- 新字段全部 Optional/有默认值 → v2 加性兼容，不触发 schema 版本升级。
- `WebSearchConfig` 加 model validator：`provider == "searxng"` 必须有 `base_url`；非 searxng 禁止 `base_url`；`engines`/`language` 仅 searxng 有意义（其他 provider 给出警告或拒绝，实现时按 StrictModel 惯例拒绝）。
- `agent.example.yaml` 同步给出 searxng 注释示例。

### 3.6 依赖策略

| 依赖 | 位置 | 理由 |
|---|---|---|
| `httpx` | 提升为 core | 已是事实主路径，消除传递依赖侥幸 |
| `trafilatura>=2.1` | 新 extra `web`（默认安装建议启用） | 纯 Python、轻、Apache-2.0；2.1+ 要求 Python ≥3.10，与本项目 3.11–3.13 兼容 |
| `crawl4ai>=0.9.3` | 新 extra `browser` | **pin ≥0.9.3**：0.9.3 修了 PDF 路径 SSRF（GHSA-q5rj-45vw-vp2g）与若干浏览器进程泄漏。会拉入 playwright/patchright 等约 50 个依赖；浏览器二进制需 `crawl4ai-setup` 单独下载（数百 MB）。未安装时 `browser.py` 惰性 import 失败即跳过 ②，行为退化为现状 |
| scrapling / playwright（直接依赖） | 不加 | Phase 3 需失败样本驱动，禁止「未来可能需要」式引入 |

### 3.7 安全与审批边界（不变量复核）

- `Risk.EXTERNAL` / `EffectKind.OBSERVE` 不变；审批矩阵不变。
- SSRF：HTTP 路径逐跳校验不变；SearXNG base_url 例外仅限配置显式目标。
- **Crawl4AI 的 SDK 没有任何内建 SSRF 防护**（其 URL 校验只在 Docker server 部署里）——必须在我们这层执行：渲染前对 URL 复用 `validate_public_url`；通过 `crawler_strategy.set_hook("before_goto", ...)` 对每次导航（含浏览器内重定向）重新做公共主机校验；可用 `on_page_context_created` 装 `context.route` 拦截子资源到内网的请求做纵深防御。浏览器内 302 到内网是残余风险，hook 方案即针对它，文档声明该边界。
- 不把 Cookie/凭据写入 Session/Artifact 摘要；Crawl4AI Session 持久化默认关闭（每次 fetch 独立 context）。
- 强风控站点：Crawl4AI 不能可靠绕过 Cloudflare（上游 issue #1757 仍开着），框架免费 ≠ 绕过风控；IP 风控/验证码超出内置工具承诺范围，文档明确。
- 微信公众号：服务端渲染居多、通常可抽取，但图片 lazy-load 且有限流；作为手动验证用例，不进 CI 承诺。

## 4. 分阶段实施

### Phase 0 — 决策记录与基线（0.5 天）

1. 在 `docs/architecture-guide/04-tools-permissions-security.md` 修订三层 Web 能力描述，记录对 2026-09-10 审计「浏览器不进内置」结论的修订与理由。
2. 给现有 `web_fetch` 分页契约、Brave/Tavily 搜索各补一组契约测试（httpx.MockTransport + host_guard 注入），作为后续重构的安全网。
3. 核查 `transcript.py` receipt 化对 dict content 的渲染，必要时先修（结构化输出的前置依赖）。

### Phase 1 — SearXNG + Trafilatura + 结构化输出（核心交付，约 3 天）

1. `web.py` 拆分为 `tools/web/` 包（纯搬迁，`__init__` 保持现有导出，外部零感知）。
2. `models.py`：三个输出模型 + 字段注释（含 `extra="ignore"` 偏离理由）。
3. `SearchProvider` 内部 Protocol；Brave/Tavily 逻辑迁入独立 Module 并映射到 `SearchResultItem`（含日期归一化）；新增 `searxng.py`（含 unresponsive_engines 诊断、独立超时、忽略 number_of_results）。
4. 两个 ToolSpec 声明 `ToolOutputSpec`；docstring/description 改写。
5. `WebSearchConfig` 扩展 + validator + `agent.example.yaml` 示例。
6. `extract.py`：Trafilatura 快速路径（`output_format="markdown"`、`include_links=True`、`deduplicate=True`，元数据走 `bare_extraction().as_dict()`），失败/正文不足回退 `_TextExtractor`；`WebFetchResult` 落分页字段。
7. `httpx` 提为 core 依赖；`pyproject.toml` 加 `web` extra。
8. 测试：
   - SearXNG：MockTransport 模拟正常/空结果/403（formats 未开）/unresponsive_engines/base_url 校验。
   - Trafilatura：fixture HTML（正文充足 / JS 空壳 / 抽取失败）三路径；分页契约冻结测试。
   - 结构化输出：ToolOutputSpec 校验成功/失败（失败→RetryPromptPart）契约测试；Brave/Tavily 映射 snapshot。
   - 配置：searxng 缺 base_url 报错、brave 带 base_url 报错。
9. 文档：README 工具章节、04 号决策记录、SearXNG 部署 checklist。

### Phase 2 — Crawl4AI 浏览器回退（约 2-3 天）

1. `browser.py`：Crawl4AI 懒加载单例、async 化 `web_fetch` 闭包、`fetch_strategy` 配置接入；`BrowserConfig(headless=True, text_mode=True, avoid_ads=True)`；读 `fit_markdown or raw_markdown`。
2. 降级判定（`js_shell`/`thin_content`/`extract_failed`/`blocked`）+ `fallback_reason`；`success=False` 显式降级。
3. SSRF：`before_goto` hook 逐导航公共主机校验（+可选 route 拦截），残余风险写入文档。
4. Host 生命周期：浏览器实例随 WorkspaceHost 关闭；验证取消/超时路径不泄漏 Chromium 进程。
5. `pyproject.toml` 加 `browser` extra（pin `crawl4ai>=0.9.3`）；安装文档说明 `crawl4ai-setup`。
6. 测试：Crawl4AI 在 seam 处注入 fake（CI 不真启浏览器）；真实渲染用例标记 live 可选。手动验证：一个 JS SPA + 一个微信公众号链接。
7. 验证链：`ruff` / `pyright` / `pytest` / `lumen.contracts --check` / Atlas `--check` / `uv build` + wheel 内 `lumen --version`。

### Phase 3 — Scrapling / Playwright（仅凭证据启动）

触发条件：收集到 ≥3 类真实 URL 在 Phase 1+2 链路上稳定失败（如 Cloudflare 挑战、强指纹检测）。届时按同一策略链模式加 `StealthFetch`，配置 `fetch_strategy` 扩枚举。本阶段不预先写代码、不预先加配置字段。

## 5. 验证矩阵

| 变更层 | 必跑 |
|---|---|
| 配置/schema | `tests/test_config.py`、`uv run python -m lumen.contracts --check` |
| 工具实现 | 新增测试 + `tests/test_download_file.py` 回归 + `tests/test_tool_registry.py`（output 契约） |
| 结构化输出的 journal/receipt 影响 | transcript 相关测试 + 受影响的 session snapshot 更新 |
| Host 生命周期（Phase 2） | `tests/test_workspace_host.py`、`tests/test_web_api.py` |
| 打包 | `uv build`，wheel 安装后 `lumen --version` + `--check-config`（含 searxng 配置样例） |
| 前端 | 不涉及（无 OpenAPI 形状变更则跳过；若 Host 契约变动再跑 `pnpm --dir src/web test && typecheck && build`） |

## 6. 风险与开放问题

1. **Crawl4AI 依赖重量**：约 50 个传递依赖 + Chromium 二进制（数百 MB）。缓解：`browser` extra 隔离；未安装时行为=现状。
2. **浏览器内存泄漏史**：上游反复修（0.5/0.7.3/0.9.x 都有相关修复）。本期单例 + 关闭钩子；周期性重建记录为 hedge，出问题再议。
3. **SearXNG 引擎可用性**：Google 引擎对服务器 IP 长期封锁，开箱体验「约等于 DuckDuckGo 代理」；文档 checklist 引导 pin 引擎集，框架不做健康检查。
4. **SearXNG SSRF 例外 + Crawl4AI 自带 SSRF 真空**：两处安全边界都是本次新增的审计面（3.3、3.7），评审重点。
5. **结构化输出的首例风险**：我们是 `ToolOutputSpec` 首个生产用户；`transcript.py` receipt 渲染、snapshot churn 已在 Phase 0/1 排入核查。
6. **Brave 字段映射**：调研未能访问 Brave 官方文档页，冻结映射前用一次真实 API 响应核对（Phase 1 任务）。
7. **开放问题（已决策，记录备查）**：不做 Codex 式多查询批量与 ref 句柄、不做 OpenAI 式引用偏移标注——属另一工具语义/Host 渲染层；若未来 TUI/Web 要渲染引用，在 Host 层从 `SearchResultItem` 投影，不回改工具结果模型。

## 附录 A：第二轮深度调研关键结论（2026-09-14）

### A.1 SearXNG 事实核查

- `format=json` **默认关闭**：`settings.yml` 需 `search.formats: [html, json]`，否则 403（by design）。
- 2026 平台变化：Redis→Valkey、WSGI→Granian、官方 in-repo `container/docker-compose.yml` 模板。
- `server.limiter`/`public_instance` 对 Agent 后端保持关闭；无内建认证，绑定 localhost 或反代 Basic Auth。
- `number_of_results` 不可靠/已移除；`unresponsive_engines` 是每请求的引擎健康信号。
- 引擎默认状态：duckduckgo/brave 启用，google/bing/baidu 禁用；Google 封锁是慢性问题（issue #2515 等）。
- 客户端超时建议 10–15s；pin `engines=` 避免坏引擎导致全查询 500。
- 来源：docs.searxng.org（search_api / settings / limiter / installation-docker）、github.com/searxng/searxng（settings.yml、issues #2987/#5034/#2515）、apiserpent.com 2026-07 实测。

### A.2 Crawl4AI 事实核查

- 最新 0.9.3（2026-08-31），Apache-2.0，Python ≥3.10；**pin ≥0.9.3**（PDF SSRF 修复）。
- pip 安装拉 playwright+patchright+playwright-stealth 等约 50 依赖；浏览器二进制需 `crawl4ai-setup`。
- 长驻应用官方推荐手动 `start()/close()` 单例；`result.markdown` 是对象，读 `fit_markdown`（有 content filter 时）或 `raw_markdown`。
- 反爬检测内建：挑战页/薄响应体 → `success=False` + `error_message`；Cloudflare 不能可靠绕过（issue #1757）。
- **SDK 无内建 SSRF 防护**（校验只在 Docker server）；hook seam：`before_goto`、`on_page_context_created` + `context.route`。
- 来源：pypi.org/project/Crawl4AI、docs.crawl4ai.com（async-webcrawler / crawl-result / anti-bot-and-fallback / hooks-auth / undetected-browser）、GitHub CHANGELOG 与 issue #1608/#1757。

### A.3 结构化输出约定对比

- Claude Code：WebSearch 只给 `title+url` 极轻列表；WebFetch 经子模型问答，主模型永不看原始页面——「搜索极轻、读取按问题驱动」。
- OpenAI Responses：引用是 answer 文本上的 annotation（字符偏移），与结果列表分离；引用渲染属 Harness 层。
- Codex `web.run`：多查询批量 + 内部 ref 句柄，属另一工具语义，不借鉴进本工具。
- Tavily/Brave 交集：`title/url/snippet/published/score`；Tavily 日期是 RFC-2822 需在 seam 归一化 ISO 8601。
- MCP 2025-06-18 起有 `outputSchema`/`structuredContent`，但 Claude Code 仍只把 text 块给模型——文本表达仍是模型实际读到的东西，印证 canonical 序列化形状的重要性。
- Trafilatura：最新 2.2.0（2026-07-31），Apache-2.0，活跃维护；`extract(output_format="markdown", include_links=True, deduplicate=True)` + `bare_extraction().as_dict()` 取元数据。
- 来源：code.claude.com/docs/en/tools-reference、developers.openai.com web search guide、github.com/openai/codex web_run_description.md、docs.tavily.com、modelcontextprotocol.io spec、trafilatura.readthedocs.io、pypi.org/project/trafilatura。
