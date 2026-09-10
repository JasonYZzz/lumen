# 15. Provider 模型能力目录与升级约定

状态：Accepted
日期：2026-09-08

## 决策与范围

`provider_catalog.py` 拥有供应商身份、协议、精确模型 ID、可用档位、兼容别名、已核对的
provider 原生联网能力及官方依据。
`models.py` 使用相同的协议归一化和端点优先级构建 SDK Model；`reasoning.py` 将选择转换成
类型化原生参数。Host、CLI、TUI、Web、child Runtime 不再按名称自行猜测模型能力。

[当前对应关系列表](../generated/provider-reasoning.md)从生产目录生成，不能手写维护另一份表。
每个 Profile 包含稳定 ID、官方文档链接和核对日期；目录版本进入 Session 和请求诊断。
这份目录描述已核对的参数契约，不代表全球全部模型、账户可用性、配额或模型仍在售。
目录版本 `2026-09-09.1` 还登记 DeepSeek V4 Flash / Pro 官方 Responses 路由的原生
`web_search` 契约。此前版本按维护范围精简：删除全部 Claude、Gemini 及 OpenAI 旧型号条目，
OpenAI 只保留 GPT-5.6（Sol、Terra、Luna，含 gpt-5.6 别名）和 GPT-6 Astra。
DeepSeek、百炼 Qwen/DeepSeek/GLM、Kimi 条目继续保留。移除条目的模型回到 unknown，
不再提供自动档位；显式引用已删除 Profile 会报错，不静默切换模型。

## 匹配规则

1. `id` 前缀确定协议 Adapter；`api` 别名统一到 Chat 或 Responses。协议不代表实际供应商。
2. 配置 base_url 优先，其次对应 SDK 的环境变量覆盖，最后才是官方默认。
   比较 HTTPS 域名、端口、路径和协议，不用 `包含 deepseek` 或 `startswith(gpt-5)` 猜测。
   百炼只识别已知 DashScope 域名及 `.maas.aliyuncs.com` 的 Anthropic 路径。
3. 供应商和协议匹配后精确查模型 ID。没有经过核对的新版本不会自动继承旧规则。
   同名模型通过不同供应商调用可匹配不同档位，例如官方 DeepSeek 与百炼 DeepSeek。
4. 已知模型的 `reasoning_levels` 只能收窄，不能增加文档未支持的档位或制造 off。
   未登记部署仍兼容原有声明方式，但来源明确是 `deployment_declaration`，不能声称官方已验证。
5. 自定义代理可用 `reasoning_profile` 引用已核对规则；精确模型 ID 和协议必须匹配。
   这是用户对代理兼容性的显式声明，显示为 `deployment_profile`；不能覆盖已识别供应商的身份。
6. `native_web_search.mode: auto` 也只匹配已核对的供应商、精确模型 ID 和协议；当前覆盖
   DeepSeek/Kimi Responses 与百炼 Qwen3.8 Anthropic server tool。目录同时记录 wire 参数差异，
   例如 Kimi K3 必须省略 `search_context_size`。未登记模型与自定义代理默认关闭；部署者可用
   `enabled` 显式声明其支持，用 `disabled` 强制关闭。
7. Provider 原生 tool 的 Part 只用于判断 replay safety，不投影成 Lumen 本地 function call。
   若兼容 SDK 缺失本地工具的 start 事件，Driver 只在拿到完整 PartEnd 后补发有序 start/completed，
   不执行半截参数，也不放宽 Loop 的协议门禁。

例如代理转发 GPT‑5.6 Sol，且部署方确认保留原生接口语义：

```yaml
id: openai:gpt-5.6-sol
api: responses
base_url: https://your-proxy.example/v1
api_key_env: YOUR_PROXY_API_KEY
reasoning_profile: openai-gpt56-sol
reasoning_effort: low
```

普通官方端点无需设置 profile。Web 设置保存和能力预览均支持此字段；输入框旁的 Session
档位选择仍使用 Host 返回的能力集合。界面不维护供应商列表和规则副本。

Web/TUI 菜单优先展示实际档位，重复兼容别名仍可用于 CLI、API 和旧配置；当前已保存的别名
以及被部署约束单独保留的别名仍可显示。Web 工具栏将默认项简写为“默认 · high”等，菜单展开
显示“跟随供应商默认（high）”及其不发送参数的语义。同一契约的两个模型可以有相同菜单。

Web 的 Session 推理状态仅对选中的任务生效；返回新任务时清空。切换模型后重新获取当前任务
的推理信息，迟到响应必须校验任务归属，不能覆盖新任务页。模型默认使用 bootstrap；任务显式
选择使用 Session 快照，不允许旧任务的 React 状态覆盖新模型默认。

## 映射语义

- `provider_default`：不发送应用指定强度；不推断服务端内部预算。
  `provider_default_level` 单独记录有官方依据的缺省档位，仅用于展示，不写入请求参数，
  也不冒充实际观测到的 effective。官方默认不等于适合所有任务的推荐值。
  DeepSeek 官方为 high，百炼 Qwen 3.8 为 xhigh，百炼 DeepSeek/GLM 为 max，Kimi Coding 为 high，
  Moonshot K3 为 max，OpenAI 5.6 为 medium。GPT-6 Astra 的档位范围已确认，当前核对资料未明确
  缺省档位，因此不猜测。自定义代理声明 Profile 不证明其缺省行为，默认值仍显示未确认。
- `off`：明确关闭，只向已确认允许关闭的模型提供。Kimi Coding 关闭时会改用 K2.6，故不提供。
- 兼容别名按供应商文档显式登记，例如 DeepSeek medium→high；不任意向上或向下钳制。
- GPT-5.6 支持 off/low/medium/high/xhigh/max；GPT-6 Astra 只支持 low/medium/high/xhigh/max。
  Astra 使用工具必须选择 Responses，Chat 仅用于文本请求，详见该行官方来源。
- Anthropic/Google 通用协议 Adapter、原始 settings 和历史参数快照仍保留，移除模型条目
  不会移除 Qwen/DeepSeek/Kimi 所需的 Anthropic 兼容协议。自定义部署声明仍按原契约解析。

原始 settings 在没有新选择时保持旧语义，未校验配置仍明确标记；同层冲突报错，高层显式选择
清理已知冲突字段。推理正文、usage、上下文恢复、审批、Sandbox、Loop 重试权威均不受目录管理。
目录也不管理上下文窗口、价格、网络重试或供应商模型发现；这些不因能力规则重构而复制实现。
原生搜索是冻结 `ModelDriverRequest` 的 native tool，不进入 `CapabilityGateway`。它的 schema 参与
tool count、digest 与请求 fingerprint；provider 原生调用会把请求标记为不可安全重放。

## 供应商升级流程

1. 核对模型和具体接口的官方文档，包括模型别名、允许关闭、有效 effort、兼容映射、端点路径。
   `/models` 的存在或返回名称不能证明推理能力；文档不明确时保持 unknown。
2. 在 `RULES` 增加或修订对应 Profile，更新来源、核对日期和 `CATALOG_REVISION`。
   新型号使用新的精确 ID；不要扩大前缀匹配。旧记录的变更也要保留在 Git 审核中。
3. 如果供应商引入新的参数语义，扩展 `ReasoningCodec` 和统一参数转换，同时添加请求级用例；
   若只是新增同契约模型，只更新目录数据即可，不需要修改 UI、Host 或 AgentLoop。
4. 运行以下检查；生成表与生产目录不一致、匹配重复、非法别名、错误协议和非法档位均须失败。

```bash
uv run python scripts/export_provider_catalog.py
uv run python scripts/export_provider_catalog.py --check
uv run pytest tests/test_provider_catalog.py tests/test_reasoning.py tests/test_models.py
uv run ruff check .
uv run pyright
uv run python -m lumen.contracts --write
pnpm --dir src/web api:schema
uv run python scripts/build_architecture_atlas.py
```

之后按仓库风险分层执行完整验证。供应商或 SDK 升级必须跑 HTTP MockTransport 矩阵，而不是只检查
设置字典；可再执行真实 Provider smoke test，但离线测试不得冒充服务端接受或性能证明。
`test_catalog_export_is_current` 将目录文档 freshness 纳入全量 pytest。

## Session 和历史兼容

`lumen.reasoning.ReasoningLevel` 继续重新导出同一个枚举，保留既有导入 Interface。
这是从原定义迁移到 Provider 目录的兼容导出；当前生产调用者仍引用旧路径，因此暂不删除。
更新全部调用者后可删除重新导出，不维护第二个枚举。

目录元数据是当前能力事实，Session 恢复时刷新；用户选择按 Session/模型保留。新 Run 开始前
按最新规则重新校验和解析，失效选择报错要求重选，不静默升级强度。已派发 child 的参数快照
继续冻结，不能根据新目录重算旧执行。Session 仍是 v10，新增可选来源字段不重写旧 journal。

本轮删除了 SDK 宽泛名称推断和 `reasoning.py` 的供应商条件链；它们已被目录完整替代。
同时删除 `models.py` 私有 API 别名表，构建与能力解析共用 `model_protocol`，避免入口漂移。

精简目录时同步删除零生产规则引用的 `anthropic_adaptive`、`anthropic_budget_effort` Codec
及其解析分支。历史快照直接保存类型化参数，不依赖这两种 Codec，加载与应用兼容测试继续保留。
Google level 和 Anthropic budget 仍被自定义部署声明路径引用，因此保留。

## 验证记录

初版验证（2026-09-08，目录版本 `2026-09-08.1`）：

- 全量 pytest：1343 passed；60 个 TUI snapshots passed。5 个 warning 来自旧 Claude Sonnet 4
  兼容用例的 SDK 退役提示；保留历史参数契约不代表该模型仍可调用。
- Web：149 tests passed；TypeScript typecheck、OpenAPI 生成和 Next production build 通过。
- Pyright：0 errors、0 warnings；Ruff 排除无关的 `scripts/chart_geometry.py` 后通过。
  全仓 Ruff 的 44 项现存问题全部位于该未改动文件（43 RUF001、1 E501）。
- Provider 自动生成表、contracts、Architecture Atlas freshness 检查及 `git diff --check` 通过。
- `uv build` 通过；从新 wheel 隔离安装执行 `lumen --version`，输出 `lumen 0.1.0`。
- 本地现有 5 个模型配置均匹配到官方规则；检查仅输出模型名与能力元数据，未修改配置。

请求矩阵使用真实 SDK 和 HTTP MockTransport 检查参数序列化，不调用外部模型。
真实 Provider 的服务端接受、账户权限、模型在售状态及性能不在离线契约测试的证明范围内。

精简目录后验证（目录版本 `2026-09-08.2`，12 组规则、18 个供应商/模型组合）：

- 全量 pytest：1292 passed；60 个 TUI snapshots passed，无旧 Claude 退役 warning。
- Web：149 tests passed，TypeScript typecheck 通过；本轮未改动 Web 源码或 API schema。
- Pyright 0 errors、0 warnings；Ruff 排除上述未改动文件后通过。
- 自动生成表、contracts、Atlas freshness 和 `git diff --check` 通过；wheel 构建及隔离安装
  `lumen --version` 通过。
- 删除旧目录专属用例，新增目录范围约束、移除 Profile 拒绝和历史参数快照兼容测试；
  CLI/Host/API 的控制测试改用当前收录模型，避免测试依赖已移除的 GPT-5 档位规则。

Web 状态与默认值修正验证（目录版本 `2026-09-08.3`）：

- 全量 pytest 1310 passed，60 个 TUI snapshots passed；新增所有目录协议的 provider_default
  HTTP 序列化用例，确认 SDK 最终请求也未补入 thinking/effort。
- Web 153 tests passed；typecheck、OpenAPI 生成和 production build 通过；Pyright 和排除
  无关 `chart_geometry.py` 的 Ruff 通过。
- 真实浏览器连接隔离测试工作区，使用生产静态产物和真实 Host 验证：Qwen 默认 xhigh →
  任务选择 low → 返回新任务恢复默认 xhigh → 切换 Kimi 默认 high，菜单仅列 low/high/max。
  刷新页面后工具栏清楚显示“默认 · high”；没有发送模型请求，也没有修改用户工作区配置。
- 已安装 CLI 为当前仓库的 editable 安装；本轮完成静态资源重建，重启 Web 后刷新浏览器可加载。
