# Lumen 第三方长期记忆后端选型

调研日期为 2026-11-25。Lumen 基线为 `eca45baca0d170c033182dfd1c0c0e700c38314c`。本次评估以当前本地优先的个人偏好、项目记忆和可配置 Agent Runtime 为前提。

## 结论

**优先验证 OpenViking v0.5.6，作为可选的跨会话语义召回服务。** 第一阶段只索引用户已授权同步的记忆条目或项目资料，通过固定 URI 写入，并在限定的范围内查询。Lumen 的 SQLite 和 MemoryManager 继续持有用户偏好、证据、确认、遗忘及撤回语义。当前本地记忆已能满足明确偏好与项目范围内的关键词召回，只有评测证明语义召回有实质收益时，才值得增加服务依赖。

**如果产品优先级变成跨用户、跨 Agent 框架的团队记忆，优先验证 TencentDB Agent Memory。** 它有更贴近这一目标的团队、成员、Agent、任务和资产装配模型，能在同一界面治理私有与共享的聊天记忆、Skill、Wiki 和 Code Graph。对话记忆可先验证独立 MemoryCore，团队资产治理再评估 Hub。另一个会改变决策的条件是 Lumen 要随产品分发服务，或者要求无 embedding 的关键词检索。腾讯的 MIT 许可和独立 BM25 路径在这两点上有优势。

### 核查版本

- **TencentDB Agent Memory** 以用户指定的 `feat/server_team` 为准。GitHub 元数据确认它是默认分支，核查提交为 `3eb5b04`。正式发布 `v2.0.4` 于 2026-11-19 发布，tag 解析到 `db13b44`，与本次分支快照不同。试验应固定具体提交或版本并对所用 API 复核。本文比较的是开源自托管实现。 [默认分支](https://api.github.com/repos/TencentCloud/TencentDB-Agent-Memory) · [分支快照](https://github.com/TencentCloud/TencentDB-Agent-Memory/commit/3eb5b04fcf6a19d9690467cbce0dbd515b239040) · [v2.0.4](https://github.com/TencentCloud/TencentDB-Agent-Memory/releases/tag/v2.0.4)
- **OpenViking** 以 2026-11-24 发布的 `v0.5.6`（`831ad81`）为准。服务和轻量 HTTP SDK 分别发版。该版本发行说明包含 Compile API 及内容快照默认行为的变化，接入应固定实际使用的接口和镜像版本。 [v0.5.6](https://github.com/volcengine/OpenViking/releases/tag/v0.5.6) · [Python HTTP SDK 0.1.21](https://pypi.org/project/openviking-sdk/0.1.21/)

### Lumen 现有契约

- **范围与生命周期。** `MemoryRecord` 保存 USER／PROJECT／PATH／AGENT 范围、来源事件、可信度、敏感性、有效期和状态。USER 范围跨项目，非 USER 记录按 canonical project ID 过滤，同一仓库的 worktree 共享项目身份。SQLite 是当前持久权威。确认、编辑、遗忘与会话重生成后的来源撤回都通过 MemoryManager。 [数据契约](../../src/lumen/context/memory/records.py) · [MemoryManager](../../src/lumen/context/memory/manager.py) · [运行时装配](../../src/lumen/resources.py)
- **学习与召回。** `memory.use` 默认开启，自动学习默认关闭。学习队列从已持久化的会话生成带来源标注及脱敏的候选，由 Lumen 做准入与合并。隐私模式暂停召回及学习并取消待处理学习。默认召回已有 SQLite FTS5。ContextEngine 也已有带独立预算的低信任外部资料入口。 [配置](../../src/lumen/config.py) · [队列及提取](../../src/lumen/context/memory/extraction.py) · [召回仓库](../../src/lumen/context/memory/repository.py) · [ContextAssembler](../../src/lumen/context/assembler.py)
- **自动化偏好。** Host 只固定 active、explicit 的 USER／PROJECT 偏好，后台执行依赖该偏好的指纹。第三方返回的相似条目、推断画像或历史摘要不能直接用于替换这项输入。 [Host](../../src/lumen/application/host.py) · [自动化架构](../architecture-guide/16-personal-assistant-automations.md)

## 对比

| 维度 | TencentDB Agent Memory | OpenViking | 对 Lumen 的判断 |
| --- | --- | --- | --- |
| 核心对象 | L0 原始对话、L1 原子、L2 场景、L3 画像，外加团队治理的聊天记忆、Skill、Wiki、Code Graph | URI 下的资源、Self／Peer 记忆、Session 和 Skill，按摘要、概览、正文分层 | 既有条目的语义索引及项目资料更贴合 OpenViking。跨框架经验沉淀和资产分发更贴合腾讯 |
| 写入控制 | `conversation/add` 录入 L0 后触发异步抽取。已查的公开 v3 原子路由提供查询、搜索、编辑、删除和计数 | Content API 可按 URI 写入并刷新索引。自动学习另有 Session 归档与提取任务 | 固定 URI 可以对应 Lumen 记录 ID。原始会话摄取会增加数据副本、重放与撤回成本 |
| 范围与鉴权 | Core v3 数据面以服务凭据加请求 scope 过滤，逐成员资产授权主要在 Hub | API key 绑定 account/user。共享资源有目录 ACL，私有记忆属 User／Peer | OpenViking 的服务身份更容易独立验证。Lumen 的项目、路径、worktree 和 child 权限仍需要 Host 映射 |
| 可观测与遗忘 | L0 接收返回消息 ID，提取另行执行。L0/L1 到更高层以及 JSONL 副本需按路径核对清理 | 归档返回 `task_id`，可跟踪提取与归档重试。删 Session 会保留抽取的记忆 | 两者都需要 Lumen 在读侧保留 tombstone 检查，并验证远端删除、索引刷新和恢复后的行为 |
| 自托管与成本 | 独立 Node 22.16+、SQLite、本地文件。BM25 可省去 embedding，提取与聚合需要模型 | 独立 Python 服务或 Docker，常规语义链路配置 embedding 和生成模型，支持本地或远程配置 | 本地单机、低配或无 embedding 时腾讯容易起步。OpenViking 试验应单独记 embedding、生成、重建索引和网络开销 |
| 发行与迁移 | 根许可 MIT。v3 以分支源码／HTTP 契约为准。存储覆盖数据库、本地文件和可选外部存储 | 服务 AGPL-3.0。OVPack 支持内容迁移、账号级备份与恢复，独立 SDK 版本需固定 | 首期从本地权威重建外部投影即可迁移。OpenViking 若涉及随应用分发或修改后对外服务，需具体核对许可义务 |

### TencentDB `feat/server_team`

- **可绕开模型代理直连。** MemoryCore 可独立运行，预置自托管配置使用 SQLite、本地文件和 BM25，部署前应通过 `/health` 核对 FTS5 可用性。完整团队方案含 Core、Hub 和 Proxy，可选知识处理，使用模型 Proxy 会增加流量、数据及错误处理的边界。分支提供 v3 Python 客户端源码，本次下载检查的 PyPI `0.1.0` wheel 仅包含 v2 客户端，没有文档中的 `tencentdb_agent_memory.v3`。 [MemoryCore](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/README.md) · [安装](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/INSTALL.md) · [SDK 源码说明](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/sdk/memory-core/python/README.md) · [PyPI wheel](https://pypi.org/project/tencentdb-agent-memory-sdk-python/0.1.0/#files)
- **服务鉴权和业务范围是两层。** 直连 Core 由 Bearer 与服务 ID 保护，L0/L1 查询再使用请求里的 team/user/agent/task，L1 默认跨会话。分支中 Gateway 的 `V3_STRICT_ISOLATION` 默认关闭，部分缺失字段会走默认值。即使开启字段必填，业务身份仍需要调用方授权。Hub 的成员和资产 ACL 对团队共用有价值，但直连 Adapter 仍需由 Host 推导范围。 [v3 鉴权与数据面](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/v3-api-memorycore-doc.md) · [Gateway 默认值](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/src/gateway/server.ts#L1054-L1083) · [L1 过滤](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/src/gateway/v2-router.ts#L1204-L1228)
- **写入与删除都需显式对账。** `conversation/add` 生成服务端消息 UUID，写成功后通知异步提取，确认收到写入响应只代表 L0 已接收。Standalone 默认路径还会附写 L0 JSONL。定向 `conversation/delete` 操作 L0 store，L1 有独立删除接口，`chat-memory/clear` 属 team＋agent 资产级清理。原始镜像、已提取原子、画像和备份属于不同验证对象，写入结果未知时重发也可能产生重复。 [写入与 JSONL](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/src/gateway/v2-router.ts#L663-L806) · [L0 删除](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/src/gateway/v2-router.ts#L1000-L1048) · [资产清理](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/MemoryCore/src/gateway/chat-memory-handlers.ts#L185-L219) · [MIT 许可](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/LICENSE)

### OpenViking v0.5.6

- **可直接索引确定的条目。** `content/write` 接受具体 URI，`replace` 可创建或更新文本并刷新语义与向量索引。`find` 可用 `target_uri` 限定查询范围并返回 URI，Adapter 只需读取有用的命中。异步索引存在可见性窗口，应按本地 ID、版本及当前权限复核，再从本地取已确认条目的正文。直接使用明确 URI 省去上传原始对话和二次推断所带来的复杂度。 [固定版本写入路由](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/openviking/server/routers/content.py#L301-L332) · [Content API](https://docs.openviking.ai/en/api/12-content) · [固定版本查询路由](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/openviking/server/routers/search.py#L383-L429)
- **账号身份、目录和记忆粒度不同。** `api_key` 模式由服务解析账号和用户身份，ROOT key 用于管理。共享资源目录支持 ACL 和组，私有资源归用户。共享资源树根有默认继承授权，项目子树的限制应显式设置并验证。Memory API 的 Self／Peer 命名空间有自己的语义，查询范围只用于缩小召回集合，隔离依赖实际身份、授权与 Lumen 的 Host 检查。 [固定版本鉴权](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/docs/en/guides/04-authentication.md) · [共享资源 ACL](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/docs/en/concepts/15-acl.md) · [Memory API](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/docs/en/api/16-memory.md)
- **提取回执和归档独立。** Session `commit` 先持久化消息归档，再用 `accepted` 与 `task_id` 跟踪提取。删除 Session 明确保留已提取的 User／Peer 长期记忆。OVPack 可导出或迁移受支持的资源及用户树，账号级备份不包含用户账号和服务 API key 注册表，导出包需要单独保护。服务运行与分发应按 AGPL-3.0 许可文本规划。 [归档与提取](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/docs/en/api/05-sessions.md#L1334-L1362) · [Session 删除契约](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/docs/en/api/05-sessions.md#L928-L936) · [OVPack](https://docs.openviking.ai/en/guides/09-ovpack) · [部署](https://docs.openviking.ai/en/guides/03-deployment) · [服务许可证](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48bbec2e1d2930ed13c/LICENSE)

## 建议接入边界

1. **本地写入，外部索引。** 在现有 MemoryManager 外围验证一个显式启用的异步 HTTP Adapter。用 Host 管理的命名空间与稳定记录 ID 做投影，远端仅收到获准的最小文本。首期采用已确认条目，外部写入状态跟踪版本并可由本地重建。若启用远程 embedding，查询文本也会经过外部服务，应独立决定其准入。
2. **读侧复核，超时回退。** 远端只提供候选 URI 或资料命中。已确认记忆按本地记录的 scope、活动状态、版本、敏感性及路径复核。项目资料按 `RETRIEVED_CONTEXT` 的外部信任和 token 预算送入。查询使用预算内的异步调用并可取消，超时、断网或索引未就绪时走当前本地召回，后台自动化偏好仍从 Host 的固定快照取得。
3. **遗忘立即生效。** 本地忘却或撤回先使远端命中不再可用，随后按 URI 同步删除或替换。持久待同步状态须能处理写入结果未知、重启以及更新与删除竞态。验收分别观察 API 不可见、索引不召回及备份保留期限。隐私模式应先拦请求，使文本根本不发往外部。
4. **按需求扩展。** 如果目标后来变成自动抽取自然对话或团队共享，单独验证带证据的候选摄取路径。它须按已持久化的 run 提取受控输入、跟踪副本、等候后台处理、保留 provenance，并确认重放、修改、撤回与用户授权语义，再决定是否启用。

## 验证口径与证据限制

腾讯 README 的 PersonaMem 从 48% 到 76%。OpenViking 官方 LoCoMo 报告约 80–83% 适用于其旧版与特定 Agent、模型和评测链路，并附复现入口。二者的数据、基线、摄取方式和 judge 不同，无法据此给 Lumen 排召回强弱。 [腾讯评测](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/3eb5b04fcf6a19d9690467cbce0dbd515b239040/README.md#benchmark) · [OpenViking 官方报告](https://blog.openviking.ai/post/openviking-benchmark-results/) · [OpenViking 评测说明](https://github.com/volcengine/OpenViking/blob/831ad81f5838d3502812a48ec2e1d2930ed13c/benchmark/README.md)

| 分开验证的问题 | 同一批测试资料与观察项 |
| --- | --- |
| 召回收益 | 用同一确认过的记忆集比较 Lumen 本地召回与外部投影，固定生成模型、问题和上下文预算。覆盖中文改写、英文代码标识、否定和更新后的冲突偏好。测 Recall@K、无关注入率和最终回答正确率 |
| 自动提取收益 | 单独用相同已脱敏会话比较各自摄取链路，统计有证据的有效条目、错误归因及重复率。使用预先标注的正确答案，将提取错误与检索错误分开 |
| 隔离与可撤回性 | 覆盖两用户、两项目、同仓 worktree、受限 child、隐私模式、忘却、重生成、索引积压与备份恢复。跨范围泄露、隐私模式外发和已撤回内容再次注入均为淘汰条件 |
| 运维代价 | 记录额外首 token p50/p95、索引就绪时间、embedding 和生成 token、CPU／内存／磁盘。注入断网、超时、重启和重复投递，核对本地回退及恢复是否完整 |

本次完成了源码、公开 API、发布版本和 SDK 分发核查，未部署服务，未调用真实模型，也未测量 Lumen 的召回效果、延迟或成本。推荐反映当前 Lumen 架构与上游接口的匹配度，实际收益需按上表验证。
