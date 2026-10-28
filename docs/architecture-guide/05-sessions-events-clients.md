# 5. 会话、事件与客户端

## 5.1 单一权威、多种客户端

```mermaid
flowchart TB
    TUI["Textual TUI"] --> Host["WorkspaceHost"]
    Browser["Next.js browser"] --> API["FastAPI"]
    API --> Host
    Host --> Journal["EventJournal"]
    Journal --> TUI
    Journal --> SSE["SSE stream"]
    SSE --> Browser
    Host --> Repo["SessionRepository"]
```

TUI 与 Web 不各自维护一套 Agent 状态。它们通过 `WorkspaceHost` 看到同一 session、run、approval 和 event sequence。

v2 助理控制面同样通过 Host：`GetAssistantState/Page/Occurrence`、`ApplyAutomationCommand`、`EditAssistant`、`ReadAssistantResource` 和偏好命令是公共 Interface。Web／桌面通过 `/api/v1/assistant` 下的端点查询持久列表与提交操作；当前每 3 秒在可见页面轮询概览，信息流／资源库使用服务端游标分页。它们不把 SSE 当作调度队列。单次执行仍使用原 RunEvent 和 Session v13；自动化实例以固定 interaction UUID 关联 turn。TUI 没有新增同等助理面板，CLI 使用 `lumen automation`，详见[命令指南](../commands.md#14-个人助理与本地自动化)。

Apple Silicon 桌面客户端位于 `src/desktop/`：Tauri 管理窗口、目录选择、Keychain 和它拥有的
Python 子进程，主 WebView 使用该进程的同源 loopback Web/API。`lumen.desktop` 通过既有 Host
命令和 snapshot 检查状态、取消 run、结束 Live；不另写 timeline、Plan 或 Agent 状态。
随包启动页的原生命令权限不授予 HTTP 主页面，导航只允许当前后台端口和随包页面。
有界私有管道只负责启动授权、凭据、ready 和关闭，启动 token 不发到网页事件或日志。
窗口关闭保留 Host；退出和项目切换先有序释放旧 Host。详见[桌面指南](../desktop.md)。

## 5.2 Command 模型

应用层命令定义在 `application/models.py`：

- session：`CreateSession`、`ForkSessionAtTurn`、`ListSessions`、`RenameSession`、`SetSessionArchived`、`DeleteSession`；
- file restore：`PreviewTurnRestore`、`ApplyTurnRestore`，要求预览摘要、明确确认及文件 revision 再验；
- run：`StartRun`、`RetryRun`、`CancelRun`；
- interaction：`QueueRunInput`、`DecideApproval`；
- settings：`SetApprovalMode`、`SelectModel`；
- capability：`InvokeSkill`、`ContextControl`、Agent/Work Product/Live 命令。

`dispatch` 的重载让每类命令拥有确定返回类型。客户端只需要理解这些 command/result，不需要操作 coordinator 内部对象。

## 5.3 Event Journal

```mermaid
sequenceDiagram
    participant Run
    participant Journal
    participant TUI
    participant SSE
    participant Browser
    Run->>Journal: append(sequence=1..n)
    Journal-->>TUI: live event
    Journal-->>SSE: live event
    SSE-->>Browser: id = sequence
    Browser--xSSE: connection lost
    Browser->>SSE: reconnect after last sequence
    Journal-->>SSE: replay missing events
    SSE-->>Browser: continue live stream
```

`TimelineEventRecord` 是稳定的 tagged representation：`type + data + sequence`。它允许公开事件进入 JSONL、恢复为 dataclass，也让 SSE 用 sequence 实现断线续传。

普通增量事件会实时进入 EventJournal；terminal event 是例外。`RunCoordinator` 先把
`RunCompleted`、`RunWaitingForUser`、`RunFailed` 或 `RunCancelled` 作为单一 candidate 缓冲，只有相同
terminal 已随 turn append 并 `fsync` 后才交给 EventJournal。若 terminal turn 写入失败，Coordinator
追加最小 failed record 并只公开 `RunFailed`，客户端与 Session 不会观察到相互矛盾的两个终态。

## 5.4 SessionRepository

会话文件是 append-only JSONL：

- 第一条记录保存 session metadata 和创建时 schema；当前新会话写 v13；
- 后续每条 turn 保存 user input、状态、messages、timeline events、approval、usage、plan、diagnostics 与 compaction；
- 每个 terminal turn 还保存有界 `request_receipts[]`：实际 provider/model route、step、instructions/messages/tools token、完整有序 tool schema digest、context fingerprint、output reserve、hard limit 和 estimated 标记；内嵌 `ModelInputManifest` 还保存 source refs/digests、stable prefix、dynamic tail、request fingerprint 与 replay eligibility，但不复制 secret、instructions、完整 messages/schema 或大正文；
- context、settings、plan、Work Product/effect、Agent Thread/event/message/result 与 Live call 都使用独立类型化 record 追加，不挤入可变聚合对象；
- 标题和归档状态由最新的 `session_catalog` record 投影；首次删除 tombstone 是终态，后到的旧目录快照不能撤销删除；不改写或物理移除 journal；
- `history_rewind` 在同一 Session 内选择目标 user turn 之前的活动 lineage；被放弃的 turn、工具结果和 compaction summary 仍留在 JSONL 供审计，但不再进入时间线、模型 history、checkpoint retrieval 或自动记忆；
- v1–v12 文件加载时不重写。首次写入 v5–v13 对应的新事实前追加 `schema_upgrade`，读取时按 marker 链得到的有效 schema 校验后续 record；
- `load` 对不完整/非法 JSON 行明确报 `SessionCorruptError`，避免把损坏文件静默解释为合法状态；
- active prefix、rolling summary 和 checkpoint 可从 turn 中恢复；V2 checkpoint 必须通过 parent/range/cursor/digest/full-history 校验，V1 只读兼容并映射为绝对 transcript 边界；
- checkpoint 状态只在 append + `fsync` 成功后发布到运行中内存；
- 分页读取用于长时间线，不要求一次挂载全部 UI widget。

`SessionRepository.load` 始终以 JSONL 字节为权威。可删除投影缓存绑定已打开文件的
device/inode/size/mtime_ns/ctime_ns，读取前后检查 descriptor 与路径身份。Repository 已知的追加
只解析新增记录；外部改变、替换或显式 `revalidate=True` 回到完整严格读取。Host 获得执行写锁后
严格复核。缓存最多 32 条，journal 字节总量代理上限 16 MiB；返回值隔离嵌套 mutable 数据。
热列表用 `SessionOverview`，热分页只复制目标 turn；冷读取仍构建完整严格投影，尚无持久化 offset
索引。防御复制增加完整 `load` 的成本，不能把列表加速外推到全历史复制。删除缓存不改写 v1–v12。

v12 增加 `turn_checkpoint`、`session_repair` 和 Effect 的可选 `interaction_id`。完整工具批次由
Runtime 回调给 Coordinator，按同一 running interaction 追加增量、递增 sequence、前缀及累计
digest。读取只形成一条 turn，terminal 校验已保存前缀后覆盖投影。checkpoint 不保存 token delta
或未提交 compaction 父节点；超过 64 KiB 的记录先用 ArtifactStore 耐久发布至 Session 目录下的
`checkpoint-artifacts/`，journal 仅保存 digest、长度和嵌套 Artifact 引用，且不索引正文。
缺失或损坏 blob、变更的重复 sequence、未配对工具消息均严格拒绝。旧二进制无法加载 v12。

v13 增加 Effect 级恢复 receipt 和 `review_pending` turn。成功副作用的 canonical output 与 effect
在同一追加边界持久化；resume 合并 checkpoint 之后的恢复证据，receipt 按出现次数消费。缺少证据的
中断 effect 禁止自动 retry。Plan review 在 terminal append 前确定为 `review_pending`，不再由 Host
事后把 completed turn 改写成另一种状态；Live 也先保存完整转写 turn，再发布 response completed。

EventJournal 的内存尾部按 JSON envelope 字节最多 1 MiB；完整显示事件保存在匿名临时文件，
冷 cursor 扫描到边界一次，随后顺序读取，近期订阅直接读内存。文件不是 Session authority，
不做重启回放；长 active run 的磁盘使用仍随事件增长。Host 在请求和订阅释放时回收：完成且无订阅
的 run 最多 32 个或保留 30 分钟，active/审批/Plan review 引用保持存活。仅留下轻量请求身份，
同 Host 过期请求返回 `run_expired`，不能重新执行；新执行、fork、restore 身份各最多 10000 个，
满额拒绝新身份。close 等待真实写入及任务结束，不把线程取消当作 fsync 已停止。

Web 编辑用户消息通过 `StartRun.regenerate_from_turn` 在一次 Host 受理中先追加 `history_rewind`，
再为替换输入追加 running turn；Session ID、标题和当前窗口保持不变。相同文本也允许重新生成。
ContextEngine 同步丢弃旧 lineage 的 checkpoint、后台压缩与 commit 幂等缓存；Session resource snapshot
和仅由被放弃后缀支持的自动记忆会失效，避免旧网页内容或旧答案回流。已发生的工作区副作用不回滚。
独立 checkpoint 分支仍复用 `ForkSessionAtTurn`，两种 Interface 不再混用。Host 在进程锁内检查待核实的
外部结果，失败时不追加 rewind；普通 StartRun 同样在建立 running 记录前检查。

失败/取消不再必然丢弃全部模型历史。Runtime 只把已完成模型/工具批次附在
`PartialRunOutcome.completed_messages`，Coordinator 追加成功后更新内存历史。Session load 要求
`completed_model_steps` 标记的数量与消息一致且通过历史有效性校验，再恢复到 active/full history。
无标记的旧失败记录保留审计语义。当前 turn 内压缩的 checkpoint 可覆盖消息前缀，加载按绝对
`source_end` 只补入未覆盖尾部；不会重复拼入已经被摘要的批次。

模型自动重试通过 `ProgressReported` 展示，用 `TextRetracted` 清除失败候选文字，不提前发出终态。
Timeline、TUI 与 Web 均支持跨 thinking 段按 Unicode 字符撤回。Host→Provider 的请求恢复与
Browser→Host 按 sequence 的 SSE 重连是两条不同链路，浏览器重连不会重新执行模型或工具。

## 5.5 TUI adapter

Textual UI 的核心职责是把 `RunEvent` 投影为 timeline：

- 33ms 合并流式 Markdown；
- 固定 Todo/Plan 面板；
- 工具卡片和审批队列；
- 有限 widget 窗口与向上分页；
- `/context`、`/memory`、`/mcp` 等 command；
- prompt history、`@file` 和 `/` 补全；
- 离开底部后停止自动滚动，保留阅读位置。

Skill、MCP Prompt/Resource、Hook 列表与 Context control 都通过 `WorkspaceHost` command；TUI
不再直接调用 `ResourceManager` 或 `ContextEngine` 的 mutation Interface。普通图片输入支持工作区内
`@path.png` 和终端粘贴出的图片路径，两者先经 `ImportAttachmentPath` 转成 canonical
`AttachmentRef`。

## 5.6 Web adapter

FastAPI 提供 bootstrap、session、run、approval、context 和文件搜索端点。安全策略包括：

- 默认只监听 loopback；
- 一次性启动 token 换取 HttpOnly cookie；
- CLI 的 `api/auth.py` 使用工作区内、按监听地址隔离的私有密钥文件保持重启后的浏览器认证；
  Cookie 名称由密钥摘要隔离，避免浏览器不按端口隔离 Cookie 导致串用；桌面私有后台仍使用进程级密钥；
- 认证交换与入口 HTML 禁止缓存；入口移除 token 并保留 session/view/id，过期 token 不签发 Cookie；
- API 401 进入独立连接恢复状态，不追加失败 turn；重试只恢复读取与事件订阅，不重放用户操作；
- SSE 通过 sequence 恢复；
- 浏览器刷新不取消后台 run；
- bootstrap 投影当前模型的 `inputModalities`，Web 在上传前禁用不受支持的图片输入；
- OpenAPI schema 生成 TypeScript contract，CI 检查漂移。

CLI 密钥不进入配置、Session、事件、后台 `web.json` 或日志；POSIX 校验所有者与私有权限，
使用有界读取并拒绝目录／文件符号链接，损坏时明确失败而不覆盖旧密钥。新启动 token 每次随机生成，
Host／Origin 校验与 Live WebSocket 同源认证保持一致。浏览器 Cookie 被清除时仍需一次性启动链接，
不因 loopback 地址自动登录。首次修复升级无法恢复旧进程已丢弃的密钥，须重新登录一次。

Web Composer 支持图片选择、拖放和剪贴板图片。浏览器使用原始二进制请求上传，Host 校验格式、大小和
magic signature 后写入 ArtifactStore；`StartRun` / `QueueRunInput` 只携带 AttachmentRef。

Web Composer 将正文编辑与底部工具栏分开；添加内容、运行中输入方式、模型、推理和审批设置集中在输入框内，
窄屏将任务设置换行并允许横向浏览。系统设置保留分类导航，模型表单按连接设置、推理与搜索、用量限制分组；
保存操作固定在滚动区底部，分类切换保留草稿，关闭时仍通过既有未保存确认流程。

Session 管理端点只向 `WorkspaceHost` 派发 command。历史列表默认过滤归档、删除墓碑和没有 turn/显式标题的空 Session；归档列表可显式请求。归档、恢复可见性与软删除在工作区 OS execution lock 内检查最新 journal；活动 run、活动 Agent 或未结束的 Live 连接阻止操作。未核实的历史 effect、待导入结果和失败记录不阻止隐藏任务，也不会因此被豁免或重放；完成与续跑仍使用原有恢复门禁。重复删除已存在的墓碑返回成功，不追加重复 record；不存在的 ID 仍返回 404。Host 访问缓存 actor 前重新检查墓碑，避免跨进程删除后继续暴露会话。Web Adapter 不直接修改 journal。

`WorkspaceHost` 接受 `StartRun` 时，会在调度模型或工具前通过 `RunCoordinator` 追加一个带唯一 `interaction_id` 的 `running` turn。未命名 Session 随后追加标题“新对话”及 `title_generation_turn` 来源索引；Host 后台读取该 turn 的脱敏、有界输入，经 ResourceManager 的无工具模型调用生成短标题。生成不阻塞主 run；失败回退到本地摘要。目录更新只追加新 record，且只有来源索引仍匹配时才能写回；手动命名清除待生成索引并取消任务，删除也取消任务。Host 关闭时取消未完成的标题请求，后续打开可从持久化索引恢复。Web 仅在列表存在 `titlePending` 时轮询目录，不重载 timeline；其他 Adapter 读取相同 Host 投影。

terminal turn 使用相同 ID 继续追加；`SessionRepository` 的读取投影以 terminal 记录取代 running 记录，JSONL 字节仍全部保留。Repository 在唯一 JSONL 写入 Seam 先使用 Pydantic JSON 语义规范化整条 record，因此 provider usage 中的 `Decimal` cost 会按精确十进制字符串持久化，日期、UUID 等标准领域标量也不要求每个调用者重复转换；不认识的不透明对象仍会使写入安全失败。若完整 terminal record 仍因其他富载荷无法序列化，Coordinator 会舍弃未提交的 Context fingerprint，并追加只含已流出 timeline 与错误信息的最小 `failed` terminal record；Host 在发出 `RunFailed` 前还会对仍处于 `running` 的同一 interaction 做最终对账。这保证已通过 SSE 展示的助手文本不会因终态持久化异常在切换 Session 后静默消失。执行中冷启动可以恢复用户输入，完成或失败后又不会出现重复 turn。Web 刷新按 URL 中的 Session ID 直接恢复 snapshot，再通过 `active_run_id` 重连同一进程内仍在执行的事件流，不以侧栏列表是否已投影为恢复前提。对于修复前已经形成的 title-only Session，只有存在 effect、Work Product 或 Agent 等执行证据时，Timeline Adapter 才从自动标题恢复有界输入并显示“回答不可重建”的中断提示；纯手动标题的空 Session 不会被误判为历史消息。

冷启动 Host 没有对应 active task 时，Timeline Adapter 把仍为 `running` 的 turn 投影为
`interrupted`，不改写 journal，也不猜测恢复 provider stream。`/retry` 会恢复该 turn 的输入、附件和已有
recovery receipts；若 `TaskWorkspace` 仍有未验证 mutation 或 `unknown` Effect，则 retry 在 Provider I/O
前安全失败，必须先完成验证、回滚或用户 waiver。文字 Run 与 Live 工具执行共用 workspace 级 OS
advisory lock；锁只保护执行期写竞争，不拥有 run 状态，进程退出后由 OS 自动释放，其他 Host 的只读
Session/Timeline 操作不受影响。

`/context` 与 Web Context endpoint 还会从最新 turn 的 durable receipts、usage、timeline 和 diagnostics
生成 `latest_run` 只读投影。它只包含计数、token/cache、耗时、compaction、工具名、digest 变化类别和
有界错误类别，不复制 prompt、模型输出、工具参数、错误正文或未知 usage 字段；非工具耗时明确标为
model/context/Host 的估算，不能伪装成精确 provider latency。

Web Adapter 将每个用户 turn 的 timeline 投影为前景回答、可读活动与原始详情：assistant 最终输出、澄清与错误保留在前景；commentary、progress 和工具动作进入活动层；thinking、压缩、Work Product 与非待处理 Agent 事件进入按需展开的原始详情。`readableActivity` 只合并连续、成功且 `callView` 声明可分组的工具，不跨失败或其他事件边界。所有正在执行的工具均标记为活动，摘要阶段来自实际工具事件。待审批卡片在 composer 上方的有界滚动区保持可操作，不随正文或过程折叠消失；成功 terminal 默认折叠但尊重用户手动选择，失败或中断默认展开。完成后的计划通过默认折叠的详情复用 `PlanPanel`；Plan 方案确认入口仍独立保留。该分组是纯客户端 presentation，不修改 Session journal 或恢复权威。

Web 回答使用连续阅读正文、按文件类型的成果卡片和每轮单一复制入口；不重复助手身份，
不把整段回答自动保存为资料。资料卡片来自当前 interaction 的持久资源，刷新后仍可读取对应文件版本。

Host 缓存 Coordinator 的 terminal 候选，完成 Work Product 索引同步、文件快照收录、目标输出关联、运行绑定清理与
排队模型切换后，释放 actor／workspace 占用，再发布唯一 terminal 并关闭 replay journal。
直接命令复用同一收尾 Interface；terminal 到达时客户端立即读取 bootstrap 不再读到该 run 的旧占用。
Web 对其他会话的 active run 定期刷新，避免仅订阅当前会话而永久保留其他会话的 busy 状态。

`toolOutput` 仅解码已知内置搜索、网页读取、文件读取与命令结果契约；未知或失败结果保留原始展示，截断与分页状态明确标记。`TurnSourcesProvider` 从成功检索、网页片段和 Markdown AST 中的正文链接派生去重来源，分别记录 retrieved/read/linked，不推断证据有效性。网页链接提供预览与来源面板，长回答和 Markdown 文档复用带目录的 `MarkdownReport`；嵌套模态只由顶层浮层处理 Escape/Tab。排队和送达反馈由既有 Host input 事件派生，临时回执不写 timeline。

普通模式的 `PlanProgress` 仅在当前 run 活动且当前 turn 有计划时，投影 composer 上方居中摘要与非模态清单，复用 `planPresentation` 和 `PlanPanel`，不创建第二套步骤状态。terminal 后移除胶囊，新 turn 未更新计划时不显示旧进度；旧 `PlanDrawer` 与页头入口已删除。Plan Mode 的 `PlanProposal` 在正文展示待审方案，`PlanReview` 在 composer 上方提供确认或修改入口；Host 的 revision 审核契约仍是唯一授权来源。审核请求通过同步引用防止重复发送，并检查当前 Session 与请求身份后才更新 UI、订阅运行；导航会使旧请求的客户端结果失效，不取消已经接受的 Host 工作。意见在请求失败时保留，在 Session 或方案 revision 改变时清空。

文档预览由 Web `DocumentProvider` 拥有临时 UI 状态，文件卡片只投影本轮成功的明确文件写入；链接本身不证明文件存在。用户点击后，经认证的 `/api/v1/files/content` 调用 `WorkspaceHost.read_document`，在线程中执行有界文件读取。macOS/Linux 逐段使用目录 descriptor 与 `O_NOFOLLOW` 拒绝符号链接竞态，拒绝隐藏/父级路径与特殊文件；Windows 缺少此安全打开路径，明确返回不支持，不退回普通文件打开。响应固定为 attachment/octet-stream、no-store，不在应用源直接执行 HTML。Web 使用独立 sandbox iframe 和 CSP 渲染 HTML/Word 静态内容；Word 与 Excel 解析器只在打开对应文件时按需加载，Excel、CSV 和 TSV 只投影有界的只读单元格文本，不执行公式或宏。浏览器 PDF 使用隔离的 Blob URL；关闭预览时 abort 请求并释放 URL。该 Interface 读取当前工作区，不创建 Session record 或第二套产物索引；历史快照仍由 TaskWorkspace/ArtifactStore 管理。

模型文本中的 think/thinking 分隔符通过 `projectThinkingMarkup` 读取投影；主对话与运行记录标准视图复用同一 Interface，后者先投影再搜索和复制。原生 thinking 的通道归属优先于文本闭合标签；原生通道及用户轮次隔离 Markdown 状态，避免未闭合代码段影响后续正文。详细运行记录、工具结果、代码与转义示例保留原文。该处理不改变 Provider history、签名或 TextRetracted 的字符偏移，不能被用作内容脱敏或安全过滤器。

只有当前未决审批才显示“需要确认”并使活动层默认展开；审批卡片不受手动折叠影响。历史工具失败或拒绝仅计入可展开的失败记录，
不把已恢复的 run 永久标成待确认。终止错误保留在前景，不伪装成成功。Web 输入框的高亮层与原生
textarea 使用完全相同的字体、字重、换行宽度和滚动位置；文件高亮只改变颜色。`@文件` 根据实际
光标位置补全，保留后续文本，在受控值提交后恢复光标，并忽略失效搜索结果；中文/空格路径使用引号。

CLI `lumen capabilities --json`、TUI `/context capabilities` 与 Web `GET /api/v1/capabilities` 消费同一个 `ResourceManager.capabilities_report()` 只读 Interface。它解释 Tool、Skill、MCP server 与 Agent Profile 的实际可见性、来源、Risk、EffectKind、ToolConcurrency、审批决定、Sandbox mode 与 schema digest；该报告不参与权限决策，因此不会形成第二套 capability authority。

## 5.7 客户端能力矩阵

| 能力 | Core shared | Web Adapter | TUI Adapter | 分类 / 说明 |
|---|---|---|---|---|
| Session、run、审批、Plan、Agent、Work Product | `WorkspaceHost` command/event | FastAPI + SSE | `HostSessionAdapter` | **Core shared** |
| Skill、MCP Prompt/Resource、Context control | 同一 Host command | HTTP control endpoint | slash command | **Core shared**；TUI 无内部 mutation 旁路 |
| 文本与交互队列 | `StartRun` / `QueueRunInput` | Composer | PromptEditor | **Core shared** |
| 图片输入 | AttachmentRef + ArtifactStore + Runtime Adapter | 文件选择、拖放、剪贴板 | `@path`、粘贴图片路径 | **Core shared**；UI 获取方式不同 |
| Responses / Chat 图片 wire format | `BinaryContent` provider boundary | 无协议分支 | 无协议分支 | **Core shared**；Responses=`input_image`，Chat=`image_url` |
| Realtime voice | Live canonical Interface | WebRTC / WebSocket controls | 无 | **Web-only** |
| 直接 `! command` | `RunDirectCommand` → Gateway（Sandbox / approval / effect 账本） | 无 | PromptEditor shortcut | **TUI-only** 入口；执行与审批权威在 Core |
| 浏览器原生图片预览/裁剪 | 无 | 当前仅附件 chip | 无 | **Planned** |
| 终端原生二进制剪贴板协议 | 无 | 不适用 | 终端通常只提供路径/文本 | **Unsupported**；使用图片路径 |

矩阵中的 “Core shared” 表示状态和行为权威在共享 Module；Web-only/TUI-only 只描述 transport 或交互
能力，不能反向成为第二套 Runtime 状态。
