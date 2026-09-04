# Lumen 本地 Agent Harness 精简升级规划

> **状态：L0–L2 代码实施与本地自动化验证完成；真实 Provider 性能基线留待发布环境采样。本文件仍不是 Accepted 架构决策。**
>
> **更新日期：2026-09-02**
>
> **产品基线：** Pi、Codex 类本地助手，不是 Dongbi v7 类企业科研知识平台
>
> **Pi 证据快照：** `earendil-works/pi@b8b873b9872db04a938fb4357b5e8e824ddc051c`，
> `@earendil-works/pi-agent-core@0.84.4`
>
> **Lumen 基线：** 当前工作区源码、契约测试、Session schema v9 与 Accepted 架构记录

> **实施结果（2026-09-02）：** 完成 effective schema/upgrade chain 修复与死代码删除；复用既有
> interrupted timeline projection，补齐 retry 附件和 unresolved Effect 门禁；增加跨平台 OS advisory
> workspace run lock；将既有 receipts/usage/timeline 投影为 `/context` / Web Context 的脱敏
> `latest_run` 诊断。没有新增 Session schema、数据库、daemon、telemetry store 或第二套运行状态权威。

## 1. 修订结论

上一版规划的方向过宽。它把 Dongbi v7 的 Domain Pack、在线 Validator、离线 Eval、Release Gate、
OTEL、PostgreSQL、Redis、Outbox、多租户和远程交付等企业平台能力带入了 Lumen 路线。这些能力本身
不一定错误，但与 Lumen 当前“本地、单用户、工作区内完成任务”的产品类型不匹配，会提高理解成本、
持久化复杂度和维护成本，却不能明显改善用户每天使用 Agent 的体验。

本次修订后的结论是：

1. **不建设新的平台层。** Lumen 继续是 local-first Agent framework，不引入 PostgreSQL、Redis、
   Outbox、租户、SLO 平台或远程调度作为当前目标。
2. **不复制 Pi Harness v2 的完整 durable interpreter。** Lumen 已有稳定 Loop、running turn、
   provider request receipts、effect journal、显式 retry 和 completion gate。进程崩溃后采用“明确中断、
   用户确认重试”，比持久化完整 program counter 更适合本地助手。
3. **不新增 Domain Pack、Eval 或通用 Telemetry 框架。** Skill、ToolSpec、Context、Work Product、
   diagnostics 和现有测试已经覆盖当前扩展需求。没有真实第二种产品包之前不增加抽象。
4. **只保留四项高价值升级：** 修复当前持久化正确性问题、明确孤立 running turn 的中断语义、
   增加轻量单机写保护、利用现有 receipts/usage 改善本地性能诊断。
5. **优先做减法。** 删除不可达代码、删除重复规划、避免新增状态权威；只有复杂度会散落到多个调用点
   时才增加 Module 或 Interface。

精简后的当前路线为 **3 个增量、约 3–6 周**。其他想法全部进入条件清单，不进入默认
交付承诺。

## 2. 产品定位与架构预算

### 2.1 Lumen 的真实使用模型

本规划按以下假设设计：

- 一个用户在一个本地项目或工作区运行 Lumen；
- TUI、Web 和 headless 是同一 Kernel 的不同 Adapter；
- 常见任务持续数秒到数十分钟，用户通常在场；
- 最重要的外部 Effect 是文件修改、命令执行、MCP/网络动作和 Git worktree 导入；
- 崩溃后首要目标是“不重复危险动作、不丢用户输入、能解释发生了什么”，不是无感跨机接管；
- 本地 Session 数量和查询规模暂时不需要独立数据库或搜索集群；
- 多 Agent 最大深度为一，child 是辅助执行，不是分布式工作流平台。

如果这些假设未来改变，应按真实需求提出新的 ADR，而不是现在预建平台。

### 2.2 新设计必须通过的四个问题

任何新 Module、Interface、record 或后台系统必须同时回答：

1. 它解决了哪个已经发生或可稳定复现的问题？
2. 能否通过扩展现有深 Module 的小 Interface 解决？
3. 删除它后，复杂度会消失，还是会散落到多个调用点？
4. 它对本地用户的可靠性、速度、安全或可理解性有多大直接收益？

不能回答这四个问题的设计不进入路线。

### 2.3 复杂度预算

本轮升级设定明确预算：

- 默认不新增长期运行进程；
- 默认不新增数据库；
- 默认不新增公开 Provider/Storage Interface；
- 尽量不升级 Session schema；确实需要时必须单独 ADR；
- 不持久化 token delta 或完整 Loop state；
- 不增加新的插件种类；
- 不让 TUI/Web/headless 获得新的内部状态旁路；
- 每个增量应能独立交付和回滚。

## 3. 外部设计中真正值得保留的部分

### 3.1 Pi Harness v2：借鉴不变量，不复制状态机

Pi 当前官方 `harness.md` 是一份详细的目标实现规格，但当前公开 `AgentHarness` 仍是 scaffold，主要
运行操作显式返回 `HarnessNotImplemented`。因此它适合作为设计问题清单，不应被当作已经证明的
生产 Implementation。

固定版本证据：

- [Pi 官方仓库固定提交](https://github.com/earendil-works/pi/tree/b8b873b9872db04a938fb4357b5e8e824ddc051c)
- [AgentHarness 实现规格](https://github.com/earendil-works/pi/blob/b8b873b9872db04a938fb4357b5e8e824ddc051c/packages/agent/docs/harness.md)
- [当前 AgentHarness 源码](https://github.com/earendil-works/pi/blob/b8b873b9872db04a938fb4357b5e8e824ddc051c/packages/agent/src/harness/agent-harness.ts)
- [当前 scaffold 测试](https://github.com/earendil-works/pi/blob/b8b873b9872db04a938fb4357b5e8e824ddc051c/packages/agent/test/harness/agent-harness-scaffold.test.ts)

Pi 对 Lumen 有价值的只有以下原则：

1. **外部动作前后要有明确 durable evidence。** 即 intent → effect → settlement 的思维。
2. **外部 Effect 不承诺 exactly-once。** 不确定动作不能在恢复时自动重放。
3. **一个 Session 同时只有一个 writer。** 先消除写入竞争，再讨论并发。
4. **故障点必须可测试。** 在 Effect 前后和 terminal 写入前后验证中断结果。
5. **索引或搜索只是 projection。** 如果以后需要，删除后必须能从 Session 重建。

Lumen 不需要复制：mutable registers、完整 operation program counter、Lane runtime、migrate-on-open
rewrite、manual public drive API 或 SQLite backend。

### 3.2 Codex：保持 Core 稳定、Surface 简单

Codex 当前 app-server 以 Thread → Turn → Item 暴露稳定生命周期，客户端通过 `turn/start`、事件通知、
interrupt、read/fork 等小接口工作；sandbox 和 approval 继续属于 Core。其价值在于多个真实客户端共享
一个稳定 Kernel，而不是协议本身越复杂越好。

一手资料：

- [Codex app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Codex v2 Thread/Turn 数据类型](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs)

Lumen 已经通过 `WorkspaceHost`、typed command/result、`RunEvent`、FastAPI/SSE 和 headless 共享 Core。
当前不需要再建设 JSON-RPC app server。只有出现 VS Code/第三方 SDK 等第二个真实进程外客户端，且
REST/SSE 无法满足时，才评估独立协议。

### 3.3 Dongbi v7：只保留 Stable Core 原则

Dongbi v7 与 Lumen 产品类型不同。可保留的只有跨产品通用原则：

- Provider 只拥有 Implementation，Kernel 拥有运行语义；
- 一个稳定 Agent Loop，不存在第二套隐藏回退；
- capability、policy、approval 和 sandbox 明确分工；
- durable fact 与客户端 projection 分离。

以下 Dongbi 内容不进入 Lumen 当前路线：科研 Claim/Citation/Evidence 模型、Domain Pack、企业 Eval、
Release Gate、PostgreSQL/Redis、Outbox、tenant/principal/quota、Webhook/MQ、平台 SLO 和远程 Subagent。

### 3.4 Lumen 本地证据

- [当前实现审计](../architecture-guide/12-current-implementation-audit.md)
- [LumenAgentLoop Accepted 决策](../architecture-guide/13-native-agent-loop-migration.md)
- [Session、事件与客户端](../architecture-guide/05-sessions-events-clients.md)
- [Dongbi v7 架构图](../research/dongbi-agent-harness-architecture-diagram-design-v7.html)
- [WorkspaceHost](../../src/lumen/application/host.py)
- [RunCoordinator](../../src/lumen/run_coordinator.py)
- [SessionRepository](../../src/lumen/sessions.py)
- [LumenAgentLoop](../../src/lumen/agent_loop/loop.py)
- [CapabilityGateway](../../src/lumen/tools/gateway.py)
- [TaskWorkspace](../../src/lumen/work_products/workspace.py)
- [AgentOrchestrator](../../src/lumen/agents/orchestrator.py)

## 4. Lumen 已经具备的能力

重新审视源码后，Lumen 并不缺一个新的 Harness 框架。当前已经存在：

| 能力 | 当前 Implementation | 判断 |
|---|---|---|
| 唯一模型—工具循环 | `LumenAgentLoop` | 保留，不重写 |
| Provider Adapter | `ModelDriver` / `PydanticAIModelDriver` | 保留 |
| 工具安全 | `CapabilityGateway` + Risk/EffectKind/ToolConcurrency | 已比 Pi 默认安全模型完整 |
| Durable input acceptance | `append_turn_started()` + `fsync` | 已有，不再造 Admission Module |
| Terminal supersession | 同 `interaction_id` 的 terminal turn 覆盖 running projection | 已有 |
| 显式失败重试 | `/retry` + recovery/effect receipts | 已有，不自动恢复未知 Effect |
| 请求证据 | `ProviderRequestReceipt` + `ModelInputManifest` | 已有 |
| Cache usage | input/output/cache read/cache write token 聚合 | 已有，不新建成本平台 |
| Schema 降载 | deferred MCP + `search_tools` | 已有 |
| Context | prepare/commit、compaction、ArtifactStore | 已有 |
| Work mutation | prepared/applied/verified/failed/rolled_back | 已有 |
| 多 Agent | `AgentOrchestrator` + worktree/evidence/import gate | 已有 |
| Session UX | turn 分页、fork-at-turn、archive、rename | 已有 |
| 多 Surface | TUI、Web/SSE、headless 共用 Host | 已有 |

因此，不应再规划完整 `RunAdmissionSnapshot`、`RunExecutionStateSnapshot`、`SessionQuery` 索引系统、
DomainPackManifest 或 vendor-neutral telemetry package。它们与当前事实重叠，或者尚无足够调用者。

## 5. 当前真正值得修复的问题

### P0. Session loader 的 effective schema 一致性

`SessionRepository` 已维护 `effective_schema`，但 `context_state`、`session_settings` 和 `plan_state` 的
部分读取路径仍使用 header `schema_version` 判断合法性。对于旧 header 后追加 `schema_upgrade` 和新
record 的 Session，这可能错误拒绝本应合法的历史文件。

这是当前代码正确性问题，不是未来架构建设。应先补测试，再按证据修复。

### P0. `RunCoordinator` 不可达重复代码

`persist_unhandled_failure()` 在 `return True` 后存在重复状态赋值。该分支控制流不可达，可以安全删除。
删除后复杂度直接消失，符合 deletion test。

### P0. 孤立 running turn 的用户语义不够明确

Lumen 在模型和工具执行前已经持久化 running turn。进程硬中断后，Session 能保留用户输入，但新 Host
没有原进程的 runtime task、approval future 或 EventJournal。此时不应尝试恢复完整 Loop，也不应让
UI 长期展示为仍在运行。

建议的简单语义：

```text
发现 running turn，但当前 Host 没有对应 active run
  → 投影为 interrupted
  → 保留原输入、附件和已落盘 evidence
  → 明确提示用户使用 /retry
  → /retry 继续遵守现有 receipt/replay 规则
```

可以通过 append 一个最小 terminal turn，或通过读取投影产生 interrupted 状态。两种方案需在小 ADR
中选择；前者审计更清楚，后者不在 open 时写盘。无论选择哪种，都不自动重发 provider/tool 请求。

### P0/P1. 同一工作区的多进程写入保护

`SessionRepository._append()` 使用 `O_APPEND` 和 `fsync`，Host 在单进程内也限制一个 active run；但
用户仍可能在同一工作区启动两个 Lumen 进程。对本地助手，最简单的答案不是 lease/fencing，而是 OS
advisory lock：

- active run 开始前尝试获取工作区执行锁；
- 进程退出或崩溃后由 OS 自动释放；
- 第二个 writer 立即得到包含占用信息的错误；
- 只读 Session 列表/历史可以继续工作；
- 不引入 heartbeat、TTL、Redis 或分布式 lease。

需要先写双进程测试，确认锁的粒度。当前 Host 本身是 workspace-wide 单 active run，因此优先评估
workspace run lock，而不是建立 per-session lease 系统。

### P1. 本地性能诊断没有形成简单用户视图

Lumen 已经持久化 request receipts、ModelInputManifest、token budget、cache read/write tokens、工具
schema digest 和 Context fingerprint。缺口不是数据层，而是缺少一个小而清楚的诊断输出。

建议先扩展已有 diagnostics/capability report 或增加一个只读 CLI 输出，回答：

- 本次 run 发出了多少次模型请求和工具调用；
- input/output/cache read/cache write tokens；
- 哪一步发生 compaction、tool schema 或 instruction digest 变化；
- 最后失败、截断或 completion rejection 的原因；
- 哪些 Effect 需要 reconciliation。

第一版直接从 `SessionRepository.load()` 和现有 receipts 计算。不要建立 SQLite index、projector、
OTEL span 或新的事实表。只有数据量证明线性读取成为瓶颈后再考虑 projection。

## 6. 精简目标架构

目标架构不增加新的运行时层：

```text
TUI / Web / headless
        ↓
WorkspaceHost
  ├─ workspace run lock（轻量 Implementation）
  └─ RunCoordinator
       ├─ AgentRuntime → LumenAgentLoop
       │                 ├─ ModelDriver
       │                 └─ CapabilityGateway
       ├─ ContextEngine
       ├─ TaskWorkspace / AgentOrchestrator
       └─ SessionRepository（append-only v9，必要时才升级）

只读诊断 = SessionRepository + existing receipts 的 projection
```

没有新的 `RunAdmission` Module、OperationStore、SessionQuery service、DomainPack manager、Telemetry
service 或 Platform Profile。

### 6.1 状态权威保持不变

| 状态 | 唯一权威 |
|---|---|
| workspace command / active run | `WorkspaceHost` |
| turn / retry / orphan recovery | `RunCoordinator` |
| model-tool loop | `LumenAgentLoop` |
| provider wire | `ModelDriver` |
| tool policy/effect | `CapabilityGateway` / `TaskWorkspace` |
| context | `ContextEngine` |
| child lifecycle | `AgentOrchestrator` |
| canonical history | `SessionRepository` |

执行锁只防止竞争，不拥有 run 状态；诊断输出只是 projection，不参与运行决策。

## 7. 三个实施增量

### L0 — 当前正确性清理（约 1 周）

目标：先把已确认的缺陷关掉，不新增功能。

实施：

1. 删除 `persist_unhandled_failure()` 的不可达代码。
2. 为以下组合增加参数化测试：legacy header、合法 `schema_upgrade`、随后追加
   `context_state/session_settings/plan_state/work_state/agent/live` record。
3. 统一 loader 和 `load_turn_page()` 使用 `effective_schema`。
4. 核对所有 append 方法是否在旧 Session 首次写高版本 record 前追加正确 upgrade marker。
5. 不顺手重构 `SessionRepository`，不拆 Module，不引入通用 transaction API。

退出标准：

- v1–v9 fixture 全部不重写加载；
- 合法 upgrade chain 可读取；
- 非法倒退、跳转或未知 schema 继续 fail closed；
- Ruff、Pyright、Session/Coordinator tests 与全量 pytest 通过。

### L1 — 中断恢复语义与本地写保护（约 1–3 周）

目标：崩溃后状态清楚、危险动作不自动重复、两个本地进程不竞争写入。

实施：

1. 定义 orphaned running turn 的 `interrupted` 投影或 terminal record。
2. TUI/Web/headless 显示同一状态和 `/retry` 建议。
3. `/retry` 继续使用已有 input、attachments、recovery/effect receipts；unknown external effect 继续阻止
   自动重放。
4. 先用双进程测试证明竞争；确认风险后增加 OS advisory workspace run lock，并保持 read-only 操作可用。
5. 增加少量关键 kill-point tests，不建设通用 manual scheduler：
   - running turn fsync 后、runtime task 前；
   - provider request 已开始但 terminal 未写；
   - tool Effect 已发生但 settlement 未完整；
   - terminal fsync 前后。

明确不做：

- 不持久化完整 Loop program counter；
- 不恢复 provider stream；
- 不自动重放未知 Effect；
- 不增加 lease heartbeat/fencing；
- 不引入 Session v10，除非 orphan terminal 契约无法在 v9 安全表达；
- 不建设跨重启的通用 client request idempotency。对于本地产品，明确 interrupted + 用户 `/retry`
  比无感自动恢复更简单安全。

退出标准：

- 重启后不存在看似永久 running、实际无 task 的 run；
- 用户输入、附件和已经持久化的 evidence 不丢失；
- retry 不会自动重复 unknown effect；
- 同一工作区第二个 writer 快速、可解释地失败；
- 一个进程崩溃后锁自动释放。

### L2 — 性能与可诊断性的小闭环（约 1–2 周）

目标：用已有数据帮助用户和开发者判断“为什么慢、为什么贵、为什么失败”。

实施：

1. 先建立固定本地任务集，测量 model request count、tool count、首 token、总耗时、input/output/cache
   tokens 和 compaction 次数。
2. 从现有 `ProviderRequestReceipt`、`ModelInputManifest`、usage 和 timeline 生成有界 run diagnostic。
3. 优先将结果放入已有 `/context`、capabilities report、Session detail 或一个窄 CLI 命令；选择调用者最少
   的入口，不同时建设多个 Surface。
4. 对 tool schema/instruction/context digest 变化给出原因提示，帮助发现 cache prefix 失效。
5. 只有 benchmark 证明某项优化有效时才改请求布局、compaction 或 deferred capability 策略。

明确不做：

- 不新增 OTEL package；
- 不新增 telemetry database；
- 不做实时 dashboard、SLO 或告警系统；
- 不做 Session 搜索索引；
- 不为诊断复制 prompt、tool schema、文件正文或 secret；
- 不为了 cache 命中隐藏真实的 policy/sandbox/tool 变化。

退出标准：

- 一次失败 run 能从现有 durable facts 生成可理解的有界摘要；
- 诊断缺省不含敏感正文；
- benchmark 能区分 provider latency、上下文膨胀、schema 变化和工具耗时；
- 没有新的状态权威或后台服务。

## 8. 明确删除或移出路线的内容

与上一版相比，以下项目从当前路线删除：

| 删除项 | 删除理由 |
|---|---|
| `RunAdmissionSnapshot` 完整执行包络 | running turn + request receipts 已覆盖核心需求；本地重启采用 interrupted |
| `RunExecutionStateSnapshot` / durable program counter | 写放大和双权威风险高；当前没有无感恢复需求 |
| generic effect protocol DTO | Gateway、TaskWorkspace、provider receipts 已有各自深契约 |
| `SessionQuery` Module + SQLite/FTS index | 当前 load/page/timeline 足够；没有规模证据 |
| vendor-neutral telemetry/OTEL conformance | 对本地助手收益不足；先用 diagnostics |
| `DomainPackManifest` | 没有第二个真实领域产品；Skill/Tool/Context 已够用 |
| Validator/Eval/Release Gate | 属于科研/企业发布治理，不是本地助手核心 |
| App Protocol / SDK | Host + REST/SSE/headless 已满足当前 Surface |
| PostgreSQL/Redis/Outbox/Object Storage Profile | 没有 HA、多租户或跨主机事实源需求 |
| Lane/item-level runtime tree | 与 AgentOrchestrator 和 turn fork 重叠 |
| lease/fencing/heartbeat | 单机使用 OS advisory lock 更简单 |
| remote executor/subagent | 没有第二个生产 Implementation |
| marketplace、签名、canary、自动学习 | 产品需求不存在，维护面过大 |

这些不是永久禁止，而是“没有证据前不建设”。

## 9. 条件清单：何时才重新考虑

只有触发以下可观测条件，才提出新 ADR：

| 候选能力 | 必须先出现的触发条件 |
|---|---|
| Durable client request idempotency | 真实远程客户端采用 at-least-once retry，重复 run 已可复现 |
| 独立 app protocol | 第二个非 Web 进程外客户端，且 REST/SSE 无法满足 |
| SQLite Session projection | 大 Session 的 load/page p95 超出预算且线性优化无效 |
| 完整 operation recovery | 用户明确要求无感重启，且 interrupted + retry 无法接受 |
| Remote execution Interface | 已确定第二个生产 executor，语义无法由现有 Workspace/Sandbox 适配 |
| Domain Pack | 至少两个真实领域产品共同需要一组稳定扩展契约 |
| OTEL | 需要跨进程/跨主机 trace，现有 diagnostics 无法定位问题 |
| PostgreSQL/Redis | 多主机 failover、多租户或 durable async delivery 成为已批准需求 |

触发条件出现也不意味着直接实施，只意味着值得研究。

## 10. 验证策略

### 10.1 必跑测试

L0–L2 涉及 Session、Coordinator、Host 和 Runtime，至少运行：

```bash
uv run ruff check .
uv run pyright
uv run python -m lumen.contracts --check
uv run pytest tests/test_sessions.py tests/test_run_coordinator.py tests/test_workspace_host.py
uv run pytest
```

若 Host/API schema 变化，再运行：

```bash
uv run pytest tests/test_web_api.py
pnpm --dir src/web test
pnpm --dir src/web typecheck
pnpm --dir src/web build
```

### 10.2 最小故障矩阵

不建设 Pi 式完整 manual drive，只覆盖 Lumen 真实高风险边界：

| 故障位置 | 预期结果 |
|---|---|
| running turn 写入前 | 没有 accepted run，也没有 Effect |
| running turn fsync 后、provider 前 | 重启显示 interrupted，可显式 retry |
| provider 发出后、terminal 前 | 不猜测 stream 恢复；只保留已 durable evidence，其余明确视为中断 |
| tool mutation intent 后、settlement 前 | 按现有 EffectStatus 进入验证或 reconciliation |
| terminal append 失败 | minimal failed terminal 对账，不丢已展示的有界 timeline |
| terminal fsync 后、客户端收到前 | Session 是完成权威，重开可恢复 terminal |
| 第二进程启动 writer | 快速失败，不写入 Session |

## 11. 实施顺序与停止规则

```text
L0 正确性
  ↓
L1 interrupted recovery + workspace run lock
  ↓
L2 benchmark + existing-facts diagnostics
```

L0 必须先完成。L1 完成后重新评估是否还有真实恢复问题；如果 interrupted + retry 已足够，就不继续
研究 program counter。L2 先测量，若现有 cache、deferred tools 和 compaction 已满足预算，就只交付
诊断，不做额外性能重构。

任何增量出现以下情况应停止：

- 需要新增第二套 run/effect/session 权威；
- 必须引入数据库或后台 daemon 才能完成；
- 为一个调用点新增 public Interface；
- 需要修改多个 Surface 才能表达同一领域规则；
- 性能优化没有 benchmark 证据；
- 兼容 v1–v9 需要重写历史文件；
- 自动恢复可能重复 unknown external effect。

## 12. 最终建议

Lumen 当前最有价值的升级不是“增加更多 Harness 功能”，而是让现有 Kernel 更容易相信、更容易理解：

1. 修复 `effective_schema` 和不可达代码；
2. 把硬中断后的 running turn 明确变成 interrupted，而不是建设完整恢复状态机；
3. 用 OS advisory lock 保证本地单 writer，而不是建设分布式 lease；
4. 把已有 receipts、usage 和 manifest 变成简单诊断，而不是建设 telemetry/query 平台。

这条路线保留了 Pi 的可靠性思维、Codex 的稳定 Core/多 Surface 思维和 Dongbi 的 “NO SECOND LOOP”
原则，同时删除了与本地助手产品无关的大部分平台设计。其成功标准不是架构图更完整，而是代码更少、
状态权威更少、崩溃行为更清楚、常用路径更快。
