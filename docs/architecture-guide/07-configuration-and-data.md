# 7. 配置、数据与生命周期

## 7.1 配置来源与合并

默认配置解析器按层读取并合并：

```mermaid
flowchart LR
    Global["用户全局配置"] --> Merge["ConfigResolver"]
    Project["项目配置"] --> Merge
    Local["本地覆盖配置"] --> Merge
    Managed["Web 受管覆盖"] --> Merge
    CLI["CLI overrides"] --> Merge
    Merge --> Trust["项目信任与 MCP 审批"]
    Trust --> Validate["Pydantic StrictModel"]
    Validate --> App["AppConfig"]
```

关键规则：

- 未知字段直接报错；
- 相对路径按声明所在配置层解析；
- secret 可以来自环境变量，不写入序列化配置；
- project-local executable 配置受 trust 控制；
- MCP 定义保留来源 scope、fingerprint 与 approval 状态；
- 单模型 `agent.model` 与多模型 `agent.models` 互斥。

Web 模型设置不重写用户维护的 YAML，而由 `WorkspaceConfiguration` 写入最高优先级的
`<workspace>/.lumen/agent.web.yaml`。该文件带所有权标记、使用 `0600` 权限并通过同目录临时文件
`fsync + os.replace` 原子发布；没有标记的同名文件拒绝覆盖。保存前先以 `ConfigResolver` 的
`source_overrides` 对完整合并结果执行严格校验，再以所有配置层内容摘要做乐观并发检查。
`--config` 独占模式保持只读。保存只更新下次启动的注册表，活动 Run 会阻止修改，浏览器会明确返回
`restartRequired`，不会伪装成运行时热切换。旧的单模型形式转换为注册表时，Module 会把原模型复制为
同名注册项并维持原默认选择；原模型若只有内联密钥则安全失败，要求先迁移到 `api_key_env`。

`ConfigResolver.resolve().report()` 是只读 observation projection：它展示最终配置、参与合并的 source 与显式字段 provenance，但不参与运行决策。`api_key`、MCP env/header、OAuth secret 等值在投影前统一脱敏；CLI 的 `--dump-effective-config` 只序列化该报告。

`docs/generated/contracts.json` 从当前 `AppConfig`、`WorkspaceCommand/CommandResult`、`RunEvent`、`ToolSpec` 枚举以及 Session schema/record authority 生成。`python -m lumen.contracts --check` 是 CI freshness gate；生成物只用于导航和漂移检测，不成为第二套运行 schema。

## 7.2 AppConfig 结构

| 区域 | 控制内容 |
|---|---|
| `agent` | 模型、指令、请求限制、并行工具、Skill 开关 |
| `tools` | builtin、Python plugin、Web fetch/search provider |
| `mcp_servers` | transport、schema 延迟、risk、effect、安全重试、OAuth、resource/prompt |
| `permissions` | 默认模式、always allow/deny；交互产生的项目永久规则另存状态文件 |
| `sessions` | JSONL 会话目录 |
| `agent.models.*.context` | 模型 profile、window/output/tokenizer 与模型级覆盖 |
| `context` | ratio 阈值、recent 上限、summary 与后台压缩策略 |
| `memory` | recall、自动学习、队列重试 |
| `agents` | 原生 Agent Profile、并发、每 Run 数量与用量限制；`delegation` 仅为弃用兼容输入 |
| `sandbox` | `workspace_write` / `disabled` 与平台 Adapter 配置 |
| `live` | Realtime Route、provider、媒体与严格 completion |
| `hooks` | 生命周期 hook adapter |

长任务配置的当前默认值：`agent.limits.request_count`、`tool_calls`、
`model_request_timeout_seconds` 均为 `null`；`model_stream_idle_timeout_seconds=300`，
`model_retries=5`，退避基础值 `model_retry_delay_seconds=2`、单次上限
`model_retry_max_delay_seconds=60`。空闲计时随数据到达延续，显式总时限不延续。
原生 `agents.request_count/tool_calls/timeout_seconds` 同样默认 `null`，请求/工具预算与父配置取
更严格值。旧有限配置和 delegation 兼容映射保持有效，不自动改写或续租。
完整字段含义、计数和排障见[第 14 章](14-long-running-recovery.md)。

这些设置不会热替换正在运行的 Python 类；源码升级后需重启 Lumen 进程。

## 7.3 ResourceManager 生命周期

```mermaid
stateDiagram-v2
    [*] --> Constructed
    Constructed --> Opening: open()
    Opening --> MCPConnected: enter MCP stacks
    MCPConnected --> RuntimeReady: build model + context + runtime
    RuntimeReady --> RuntimeReady: select_model transaction
    RuntimeReady --> Closing: close()
    Closing --> [*]
```

构造阶段发现 Skill、注册本地工具、构造 MCP bundles 和 metadata。`open` 才建立远端连接并创建 runtime。模型切换使用事务式 rebuild：每个模型按“显式字段 → 显式 profile → 精确 slug alias → 80k conservative fallback”重新解析 `ResolvedContextPolicy` 和 token counter，再在局部 candidate Scope 中创建并完整打开新的 ContextEngine、`PydanticAIModelDriver` 和唯一的 `LumenAgentLoop` Runtime。成功后以一次无等待赋值同时发布 Runtime、Scope、Memory extractor 与活动模型名，再关闭旧 Scope；构建失败不修改共享引用。Host 状态锁串行化 start/switch。session 的 rolling checkpoint 不丢失；从大窗口切到小窗口时，新 Engine 会立即按新 hard limit 安全降级或在 provider I/O 前明确失败。

可逆注册由内部 `RegistrationScope` 持有。它只管理 Tool/Hook/MCP/Capability disposer 和后台 task 生命周期，不保存 Session、Plan、Agent 或 Work Product 状态。模型切换先在候选 runtime Scope 内完成构建与验证，再原子发布并关闭旧 Scope；关闭会取消并等待 task、以 LIFO 执行所有 disposer，单个清理异常不会阻断其余清理，并只保留有界 diagnostic。同一 `ResourceManager` 重开或重复切换不会累积 MCP schema、toolset、listener 或 capability。

`openai:` / `anthropic:`、`api: chat/responses` 和 `base_url` 只描述传输，不参与模型能力推断。
`settings.max_tokens` 会被完整预留并原样用于 Provider 请求；未显式配置时，resolved profile reserve 会被
写入请求。未知模型按 window 自适应选择初始 reserve（80K fallback window 对应 16K，大窗口最多 32K，
且始终不超过 window 的 1/4），并可在 length stop 后有界增长。若配置超过已知 profile 的架构输出
上限，配置加载失败。旧 `context.soft_token_limit` / `keep_recent_tokens` 是兼容覆盖，并在 `/context` 中标记。

## 7.4 主要持久化位置

| 数据 | 默认位置 | 语义 |
|---|---|---|
| session | `.lumen/sessions/*.jsonl` | append-only 对话事实 |
| artifacts | `~/.lumen/artifacts/` | 大型工具输出内容寻址存储 |
| memory authority | `~/.lumen/state/memory.sqlite3` | 长期记忆权威库 |
| memory queue | `~/.lumen/state/memory-work.sqlite3` | 后台提取任务 |
| memory projection | `~/.lumen/projects/<id>/memory/` | 人类可读审计投影 |
| approval rules | `~/.lumen/state/approval-rules/<id>.json` | 项目级跨 Session allow 规则 |
| Web managed config | `.lumen/agent.web.yaml` | 机器管理的模型覆盖；不保存密钥明文 |
| MCP OAuth | session credential root 下的受限文件 | refresh/access token |
| contract catalog | `docs/generated/contracts.json` | 可重建的 schema/枚举导航投影，不是运行权威 |

## 7.5 数据信任级别

Context block 记录 source、trust、retention 和 evidence。系统指令、用户输入、本地已验证结果、模型输出、外部 MCP 内容不应被视为相同信任级别。尤其在自动记忆学习时，未经本地验证的外部内容不能独立提升为 durable fact。
# 配置与数据
