# v2 个人助理与本地自动化

状态：Accepted，更新于 2026-10-23；说明当前源码实现，不代表已发布或完成跨平台发行验收。
产品版本为 2.0.0；配置仍为 v2、Session 仍为 v12，旧会话保持原有加载与追加升级规则。
此前[代码审查](../research/2026-10-20-v2-code-review.md)的四项问题已修复并补回归，实际验证及未验收范围见[本轮实施记录](../research/2026-10-21-v2-product-flow-validation.md)。

依据[产品研究](../research/2026-10-21-muse-chatgpt-space-product-research.md)和[实施计划](../plans/2026-10-21-lumen-v2-product-flow-upgrade.md)，现已贯通目标、关联对话、资料、成果和持续跟进。后台联网采用 Host 先读取受限来源、模型再整理快照的实现；没有开放通用后台浏览或 Provider 原生搜索。

## 产品入口与范围

Web／桌面保留 Fold Logo、Geist 与动态九尾狐，默认采用樱粉画布、白色阅读面和灰紫操作色；欢迎页外观滑块提供五种浅色背景与独立狐形偏好，不改变模型或推理设置。主导航为对话、信息流、灵感、目标、资料空间，手机固定显示五项带文字导航。全部持续跟进从全局活动入口管理，也可在目标详情试跑、启用、暂停和查看最近结果。
新建目标只要求标题和可选说明，不包含自动化选择器或预写计划。“创建并规划”先保存目标，再经 `onPlanGoal(goalId, requestId)` 复用固定关联 Session 与原 StartRun 提交一次普通规划请求。原始说明、来源会话及已有讨论草稿保留；持续推进和定时跟进分别显式启动。编辑目标仍只保存修改。“继续讨论”仍准备草稿，由用户发送。成果文件自动关联，普通问答不自动创建页面；不再提供执行后发布整段回答的快捷入口。

保存后启动失败显示“目标已保存，规划尚未开始”，重试复用同一保存结果及规划身份 `goal-plan:<requestId>`；Host 沿用既有 admission 去重，不重复创建目标或执行未知请求。刷新不自动规划，切页后的迟到响应不拉回原会话。新请求在繁忙时拒绝并保留重试；响应丢失后重试原身份仍可查询同一次 admission。

灵感由可解释规则生成：未完成目标、可用文档成果，以及明确标注的通用起点。任务建议携带来源、预期成果与步骤；关联任务携带版本化 ContextRef。
正在推进的目标不重复建议启动；待验收目标直接打开详情，未验证的持续跟进打开原配置。查看方案只展开内联内容，
通用任务先要求范围；用户明确点击“开始任务”才经既有 StartRun 执行一轮普通任务，沿用审批与沙箱，不自动启动持续目标推进或调度。
关联建议复用原讨论及其全部引用；启动前核对建议是否仍存在，Host 核对版本与对象可用性。失败保留方案与同一提交身份，
不替用户验收或把浏览页面当成执行授权。无需调用模型生成建议，没有伪装成已理解用户习惯的个性化内容。
目标是持久意图及用户确认的里程碑，不是某轮运行的 Plan，也不是调度定义。里程碑逐项编辑，保留稳定 ID、顺序与完成状态；新增项使用新 ID，删除不把完成状态转移到下一项。目标完成由用户确认，编辑过程中保留草稿并通过 revision CAS 保存。详情随 overview 刷新，过期详情响应不能覆盖新导航。
持续跟进仅在目标详情的可选折叠区域单独设置；基本目标编辑原样保留已有 automation_ids，不新建、启用或暂停安排。旧关联与全局管理能力保持兼容，目标列表只展示摘要，详情不再重复渲染同一张列表卡片。

### 显式目标推进与验收

Goal.status 是未完成／已完成／已归档的业务状态；可选 execution 保存成功标准、推进轮数上限与累计轮数、最近 run、
有界核验记录和 active／paused／blocked／budget_reached／review／accepted 状态。旧 Goal 无此字段时仍可加载，
不升级 SQLite 表或 Session schema，也不自动激活旧目标。总轮数上限由用户明确设置，留空不设上限，原每 run 预算仍有效；
不把轮数解释成模型请求数、token 或费用上限。

`ControlGoal(GoalCommand)` 为根 Host 的 start／resume／pause Interface，Web 使用
`POST /api/v1/assistant/goals/control`，认证、Origin、owner 与 revision CAS 继续生效。
start／resume 要求成功标准、未完成目标、可用关联 Session 和 Default 模式；Plan 模式不启动自动推进。
控制 request identity 和 digest 持久化，重复请求不续租预算、不重放工作；恢复保留已消耗轮数。
一个工作区仍只有原执行席位，不增加第二个 Runtime 或 Scheduler。每轮沿用 StartRun → RunCoordinator → Runtime；
只有在 Coordinator 持久化、成果收录与执行席位释放后，Host 才尝试下一轮。新工作抢先占席时暂停目标，避免争抢执行。

仅推进中的根 run 提供 `report_goal_progress`，不授予 background／child／Plan。它只记录本轮核验候选，无外部动作；
`complete` 必须引用本轮实际成功工具的 call_id，未知／失败回执拒绝。可用 inspect_personal_assistant 查看最近至多
200 份回执索引；管理与自报工具不算实际进展。后续实际工具或新用户消息使旧报告失效。
原 CompletionGate 仍决定单轮是否成功；Host 只在成功终态后采纳报告。continue 没有实际工具进展、报告缺失、
运行失败、待处理消息或收录故障时不续跑；预算用完进入 budget_reached。complete 进入 review，最终由用户验收，
明确完成后变为 accepted，不把模型自评或仅有工具回执当作业务成功的形式证明。
Goal 最多保存最近 20 轮有界核验摘要及 call_id，完整正文和证据仍在原 append-only Session。

暂停先持久化再取消当前 run，旧终态不能覆盖暂停或新激活；原权限、Effect 对账和取消清理不变。
Host 重启在取得工作区执行锁后将遗留 active 标为 paused，显式恢复才继续；不透明接管或重放未知外部操作。
目标范围修改和收尾先暂停推进；正在运行的关联工作须停止或结束，里程碑和状态修改仍走同一 Store。
有已启用跟进的目标完成／归档必须提交 follow_up_policy=pause 或 keep，事务内处理目标、调度版本及 queued 取消；
已开始的执行不被软删除或归档代替取消。只存在普通目标时仍可直接人工确认完成，不强制启动 Agent。

从对话创建的 AssistantEdit.source_session_id 经 Host 验证所属工作区、可用性与归档状态，创建时绑定原 Session。
discussion 可保留多个 ContextRef，不覆盖已有资源或目标引用；旧单 context 记录仍可读取。
删除／缺失目标和资源不进入原会话自动上下文，显式对象入口仍要求恢复；原 journal 和讨论可继续使用。
资料支持空白页面、20 MiB 以内原始文件导入、收藏、集合、收录／隐藏、版本读取及 CAS 编辑。可解码的 UTF-8 内容进入有界文本索引，二进制文件保留原字节并按受支持格式预览。资料讨论桌面双栏、手机切换；AI 回答是候选，用户明确应用后才写入新版本。通用编辑拒绝 Work Product，原修改仍走 TaskWorkspace。
个人偏好放在设置内，确认／编辑／遗忘都经既有 MemoryManager；推测候选不等于确认偏好，已遗忘的相同内容不会静默复活。
偏好保存及刷新期间锁定输入、范围与取消编辑，避免成功回调清空保存期间新输入的草稿；失败后恢复编辑。
关闭记忆或处于隐私模式时，后台不召回或固定这些偏好。

持续跟进支持 workspace_snapshot 文件快照和 research_readonly 受限研究，结果送到站内信息流。后者需要配置内建 web_search 并明确允许后台读取；仅有模型原生搜索或 MCP 连接不等于具备该能力。
不含日历／邮箱 OAuth、外部发送、Webhook、远程 Host、系统通知或自动登录项。普通交互仍使用原有工具、审批和沙箱。
这些后续能力有各自的授权与恢复门禁，不能由当前收件箱代替。

## Module 与权威

| 状态 | 唯一权威与 Interface |
| --- | --- |
| 定义、实例、checkpoint、投递、目标、资料集合／关系与讨论关联 | `automation/store.py`，SQLite 本地事务；严格 Pydantic 输入 |
| 时间与排队 | `automation/engine.py`，单个 OS owner；调用 Host，不直接执行 Runtime |
| Session、执行席位、固定 identity、工具管理命令 | `application/host.py`，WorkspaceCommand / CommandAcknowledged |
| 运行与终态 | 原 RunCoordinator、AgentRuntime、LumenAgentLoop；Session v12 的 interaction_id 关联 occurrence |
| 来源权限与 Hook | 原 CapabilityGateway；派生受限快照读取能力，不能添加父能力集合以外的工具 |
| 偏好 | MemoryManager；SQLite 自动化库只固定本次已确认偏好，不成为第二个偏好权威 |
| Work Product 正文与修改 | TaskWorkspace、SessionWorkState；资源库只索引 canonical 引用 |

控制面落在 `<sessions.directory>/assistant/<canonical-workspace-sha256-prefix>/state.sqlite3`，同目录有独立 ArtifactStore。
助理库 schema v2；识别到完整 v1 布局后先用 SQLite backup 保存受限权限备份，再在事务中增加关系、资源版本、活动索引与 admission 表。失败不提升版本；未知版本和不完整布局不自动重建。Session v12 和配置 v2 不变。
默认 Session 目录仍是 `<workspace>/.lumen/sessions/`。即使多个工作区共用自定义 Session 目录，助理状态也按 canonical workspace 分离。
只在默认的工作区目录索引旧 Session；共享自定义目录只索引本 Host 显式登记的 Session，避免自动收录其他工作区产物。
数据库损坏、缺失关键表、未知无版本布局、版本不支持或结算故障会停止新执行并显示存储错误，不重建覆盖数据库，也不阻断普通对话。
数据库打开失败会关闭连接，重试仍完整校验；启动恢复失败会关闭 Store 并释放 owner 锁，Host 可正常退出，其他 Host 可重试恢复。

## 定义、时间与执行

定义默认未启用。创建／修改／启用／暂停／试跑／立即运行有持久 request identity 与 payload digest；相同请求返回原结果，内容不同返回冲突。
修改和控制操作使用 expected_revision CAS；执行语义改变使验证失效并取消旧排队项。仅改名称／通知可保留验证和安排；启用在同一事务更新旧 queued revision，begin 再读取当前实例，不用陈旧实例撤销新状态。试跑不推进正式 checkpoint、不进入正式信息流。
成功试跑绑定意图、来源、时间、预算、模型、权限、来源 Tool Contract、Profile 和显式偏好指纹。启用及每次 admission 都重新检查；变更后须重新试跑。

时间支持 daily／weekdays／weekly 与 IANA 时区，周一为 0。DST 空缺移到首个有效分钟，重复时刻取第一次；本地日期 edition 唯一，修改时间不能让同一天重复定时触发。
启用只计算未来触发。退出／休眠后只合并到最近到期的一份，不补发全部历史。结束时间到达后停用。
时区数据依赖标准库 zoneinfo 与 tzdata；跨平台依据见 [Python zoneinfo 文档](https://docs.python.org/3/library/zoneinfo.html)。

一个工作区只有一个 Scheduler owner，和现有 workspace execution lock 分开。第二 Host 可读但不夺权。
已开始的后台任务不会被前台抢占；未 admission 的任务遇到前台／Live／其他进程持锁时保持排队，执行结束后让出事件循环。
暂停只清理 queued；停止运行中的实例使用现有 CancelRun，等待实际终态，不把卡片隐藏当成停止。

每次执行固定 Session UUID、interaction UUID、定义快照、偏好快照、时限与验证指纹。
BackgroundExecution 经 Coordinator 传入 Runtime，ContextVar 隔离本次执行；不修改共享 limits。
两种方式的模型阶段均无工具 schema、无实际 Gateway 能力、无 Provider 原生工具、无子 Agent／管理工具；显式请求预算默认 6 个模型步骤、工具预算 0、整体墙钟时限默认 180 秒。研究来源由 Host 在这段时限内通过父 Gateway 的受限副本读取，不把读取权限授予模型。
步骤内重试仍由原 Loop 管理并受总执行时限限制，不能据此保证美元硬限额。附加标题生成、压缩和记忆学习不在后台额外调用模型。
普通交互的工具和预算在后台退出后保持原行为。根交互可通过审批后的 Host 工具管理自动化和目标；后台／child／Live 不获得这些管理工具。

## 来源与结果

文件方式要求父 Gateway 的 builtin read_file，契约为 read／observe 且无需确认；不会用同名插件替代。研究方式只接受 builtin／builtin:web 的 web_search，以及可用时的 web_fetch；必须 observe、未被策略拒绝且不需要临时确认。实际检索工具为 external 风险，需用户配置显式允许，不能仅凭 observe 扩权。保留原权限、Hook、超时和公共 URL 防护；无人值守不等待内存审批 Future。
文件读取使用 descriptor／no-follow 边界，禁止绝对路径、父路径、隐藏文件和符号链接逃逸。Windows 仍拒绝文件方式，不用普通路径打开降级；研究方式不依赖该文件读取。
最多 16 个来源，每份 32 KiB，总共 64 KiB；超过限制、非 UTF-8、无法读取或敏感内容会形成失败 receipt，不能被当成没有变化。
来源正文进入 ArtifactStore，receipt 保存路径、摘要 revision 和引用；模型只得到有限快照并被声明为不可信资料。
当前敏感内容检测较保守，也可能拒绝含高熵标识符的普通文本，应在运行详情检查覆盖范围。

研究以委托前 2000 字作为固定查询，最多取 5 条检索摘要；有已授权 fetch 时最多读取其中 3 个 URL，每页最多 8000 字。receipt 保存实际查询／URL、读取时间、覆盖类型、错误和正文引用；搜索摘要不冒充全文。空结果／引擎错误不视为无变化；未知发布日期必须明确。Provider 原生搜索和 MCP 不进入这个 Profile，缺少来源时明确失败，仍可回前台执行。

完成状态区分 complete／partial／no_change／needs_user_action／failed／cancelled／interrupted／budget_reached／skipped。
缺失部分来源只产生 partial，不使试跑验证成功；全部缺失则待处理。只有成功读取的正式结果才推进对应来源 checkpoint。
成功的新执行保留投递，不再由来源哈希推导业务 no_change；旧 no_change 记录仍可加载。`notify=false` 的成功结果在信息流默认已读，Web 编辑保留此值；需处理项仍未读，不触发操作系统通知。
报告及笔记编辑使用 revision CAS，历史 occurrence 始终引用执行时原正文；查看执行结果与编辑当前资源可分别追溯。
Work Product 列表只引用原正文，通用资源编辑不能绕过 TaskWorkspace；原会话删除后索引标记不可用。
信息流和资料空间提供服务端游标分页、类型与来源 Session 筛选、标题及已索引正文搜索。资源活动索引只因内容变化推进，不因启动扫描刷新历史。新笔记、UTF-8 文件和后台结果索引正文；旧未索引材料仍可按标题找到。列表／网格共用查询，收藏、集合和隐藏不删除原文件或 journal。

## 工作上下文与成果发布

Goal 与 Resource 各有固定 primary_session_id；Store 先预留 UUID，再独占创建 Session，重启可完成尚未创建的关联。已创建会话被删除／缺失时不重新生成替身。ContextRef 严格区分 goal／resource／session，固定版本可查；跨工作区引用拒绝，Session 引用必须属于本 Host 登记的集合。

Host 解析关联资料为有界、不可信的模型上下文，保留对象 ID／版本；Goal 最多预读 3 份参考资料的节选，其他资料保留明确引用。ContextEngine 沿用预算路径。关联执行持久记录 request identity、固定 interaction、引用及上下文摘要 hash，重启重试不重复执行未知请求。

公开 Interface 包含 GetGoalDetail、ContinueAssistantDiscussion、PublishAssistantResource，StartRun 接受 context_refs；旧 publish_result 字段保留可加载，但不再触发整段回答自动发布。关系、集合与可见性编辑复用 EditAssistant。Web 只调用 Host，不直接写这些状态。深链支持 view=goal／resource 与 id，旧 session 链接继续工作。

Host 在 terminal 发布前收录实际产物。已验证 mutation 的 Work Product 自动进入资料空间；输入材料、未验证及待对账产物保持原边界。通用工具的文件交付仅提取成功 completed turn 最终回答中的本地 Markdown 附件链接，最多 32 个；必须有成功工具执行，文件修改时间不早于本次 run 开始前 1 秒。只读旧文件、普通问答和远程链接不自动变成资料，不扫描整个工作区。Work Product 路径由原权威处理，不通过通用收录绕过校验。

文件读取复用 descriptor／no-follow，拒绝路径逃逸、隐藏／敏感文件、符号链接与超过 20 MiB 的文件；Windows 仍 fail closed。通用文件原始 bytes 进入助理 ArtifactStore，Work Product 继续引用自己的 ArtifactStore。Resource 持久化 file_path、file_type、byte_size、源 Session／interaction；同一路径内容变化增加 revision，旧快照保留。当前目标以 output 关系自动关联，不将目标自动标记完成。收藏、隐藏、保存状态不因收录被覆盖。

文件内容 API 通过 Host 校验资源、所属会话与 revision，只读快照而不接收任意文件路径；已删除会话不可读。下载使用 attachment、octet-stream、nosniff 和 sandbox CSP。Web 按类型复用文档／安全 HTML／图片／表格等查看器；HTML 正文固定版本，关联资源仍来自当前工作区并明确提示。二进制只向模型提供文件元数据，不伪造提取正文。

首次收录或内容形成新版本时，Store 在资源／目标关系事务内记录 `output:<resource_id>:<revision>` 信息流事件，指向实际资源及固定版本；已索引的本轮 Work Product 走同一记录路径。比较最近事件所引用版本的正文引用，避免仅重命名、重复索引或无变化读取形成新成果通知。软删除墓碑继续生效。查看本次成果读取固定版本；围绕成果讨论优先携带该资源引用，继续目标才恢复目标关联会话。

目标收尾进入 review／blocked／budget_reached 时，以目标、激活身份和最近 run 去重记录待处理事件。attention 查询在分页前匹配当前 execution 状态及同次 activation／run；解决、重新激活或进入新轮后旧事项退出待处理集合，原事件、已读状态和历史仍保留。普通问答、试跑与无变化检查不生成成功成果通知。

显式 PublishAssistantResource 保留给调用者主动创建笔记或将回答应用到既有页面；读取持久 completed turn，固定 answer:interaction_id 防重复，expected_revision CAS 防覆盖。该兼容 Interface 不再由对话自动调用，也不提供保存整段对话的默认按钮。普通页面历史版本和原 Work Product 编辑边界均保留。

## 管理与回收站

Goal／Resource 的可选 deleted_at 在旧数据中默认为空；不改变 Session journal 或 SQLite 表结构。
EditAssistant 的 trash／restore 与 rename_resource 使用 expected_revision CAS，并对重复目标状态幂等。
默认资料查询与目标详情排除回收站资源；trash 查询可读取删除元数据，读取正文、历史版本和继续讨论均要求先恢复。
目标关系、集合关系、原始文件、正文 Artifact 和历史版本不随软删除移除。Work Product 重建索引及同路径文件再次收录
保留墓碑与用户显示名称，防止旧资料在下一轮同步复活；恢复后关联重新可见。编辑接口拒绝用普通页面文字覆盖文件快照。

信息流的分页、直接读取与 overview 共用 Store 的关联可见性条件：resource_id／goal_id 存在时，
对应对象必须存在且未删除。条件在筛选、排序和 LIMIT 前执行，适用于 all／attention／unread，
避免隐藏记录占页造成空页或漏读；没有对象关联的正常失败／待处理通知仍然保留。
feed 原始记录、sequence 和 read 不删除，全部关联对象恢复后重新可见。overview 仍携带墓碑以供回收站使用，
Web 目标侧栏显式排除它们，Engine 只从未删除的进行中目标生成灵感。自动化 settle 也只关联未删除目标，
防止后续独立执行因旧关联而消失在信息流。

删除目标要求没有关联的前台执行、启用的持续跟进或 queued／running／settling 后台执行；用户先暂停并等待，
不把隐藏目标当作取消执行。删除集合只事务移除集合及其分组关系，保留全部资料。

原文件导入经认证与 Origin 校验的流式 POST，读取时即限制 20 MiB，Host 再验证文件名、内容边界与敏感文字。
UUID 请求身份用于重复导入幂等，不同内容复用同一身份拒绝；原始 bytes 存助理 ArtifactStore，既不创建假对话，
也不把上传文件写入工作区。预览／下载继续使用固定资源版本与已有安全边界。

## 原审查问题的处理（2026-10-21）

| 原问题 | 当前处理 |
| --- | --- |
| 排队后启用 | 事务更新 queued revision，admission 重读；陈旧错误不能清空不同执行定义的有效验证。 |
| 结果去重 | 删除纯来源 hash 的 no_change 判定，成功结果正常交付。 |
| 静默设置 | 编辑保留 notify=false；组件回归覆盖。 |
| Host 工具重名 | 四个名称加入现有保留名校验，启动时给出明确冲突诊断；不覆盖插件。 |

历史反例保留在[原代码审查](../research/2026-10-20-v2-code-review.md)，当前验证以[本轮实施记录](../research/2026-10-21-v2-product-flow-validation.md)为准。

## 崩溃与持久性

SQLite 使用 WAL、synchronous FULL、事务与唯一键。单个本地 OS owner 不使用超时接管，进程退出由 OS 释放，因此没有旧 worker 和新 lease 并存的 fencing 协议。
站内 resource／feed／checkpoint 在一个结算事务内完成，不引入外部 outbox 或投递 Adapter。

| 中断点 | 恢复 |
| --- | --- |
| 定时事务前／后 | 唯一 edition 去重；queued 可安全 admission |
| running intent 已写，Session 尚未建立／turn 尚未开始 | interrupted、要求用户处理；不自动重放模型 |
| turn 已开始，终态未持久化 | interrupted，沿用 Session 诊断；不从 UI 推测成功 |
| Session 终态已写，自动化结算未写 | 从固定 interaction_id 恢复已持久化结果 |
| Artifact 已写，结算事务未完成 | SETTLING manifest 恢复，幂等重试结算，不再次调用模型 |
| Artifact 不可用 | 失败，不发布虚假成功资源 |

固定 UUID 创建仍采用独占文件创建，并在 POSIX fsync 父目录；Session 历史不会被重写。
事务与恢复测试证明这些逻辑路径，不代表真实断电、磁盘满或长时间压力实验已经完成。

## 升级、回退与发布门禁

安装前停止旧 Host，备份整个 `.lumen/`（或配置的 Session 目录），先构建 Web 再打包 wheel；CLI 版本与桌面元数据为 2.0.0。
旧配置和会话无需手动迁移。新定义默认关闭，升级不会创建或启用任务。
回退先停止新 Host、备份新助理库和 artifacts。只读旧 v1 助理库的旧包不能直接打开 v2：需恢复迁移备份到单独副本，并配套保存新写数据，不能覆盖原件或手工降低 user_version。旧配置、journal 不重写。

历史验收见 [v2 首轮记录](../research/2026-10-20-v2-implementation-validation.md)，当前以[本轮记录](../research/2026-10-21-v2-product-flow-validation.md)为准。已完成单个真实配置模型的目标／资料／成果 smoke；它不等于联网研究、所有 Provider 或性能验收。
发布仍需目标平台 CI、桌面 app／DMG 与设备验收、长时间调度和断电实验。
连接器、持久审批和外部投递属于后续独立实施；不能把产品版本号 2.0.0 当成这些能力已经完成的证明。
