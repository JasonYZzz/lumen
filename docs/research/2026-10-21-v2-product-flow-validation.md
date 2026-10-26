# v2 目标、资料空间与持续跟进闭环实施验收

日期：2026-10-21。范围：[目标驱动与资料空间优化计划](../plans/2026-10-21-lumen-v2-product-flow-upgrade.md)在本机的源码实现、回归、浏览器和真实模型验证。现行运行契约见[第 16 章](../architecture-guide/16-personal-assistant-automations.md)。这是本地验收记录，不是跨平台发行证明。

各节按实施批次记录，早期入口及限制由后续修复补充；目标创建见第 11 节，原对话关联与显式推进见第 12 节，可执行灵感见第 13 节，统一模型与思考组件见第 14 节。

## 1 已落地的用户流程

- 五项主导航为对话、信息流、灵感、目标、资料空间。持续跟进从全局“活动与持续跟进”或目标详情进入；独立的旧安排仍可找到、暂停和检查。
- 目标只要求意图，可没有文件、里程碑或跟进安排。创建后进入固定关联对话；“现在整理一次”才实际执行并发布成果，“继续讨论”只恢复上下文，不暗中启动模型。
- Goal、Resource、Session 用严格 ContextRef 串联，Host 验证工作区所有权及固定版本；有界正文作为不可信参考交给原 ContextEngine。普通无引用对话保留原入口。
- 回答从 canonical journal 的完成 turn 发布；同一 interaction 幂等生成同一成果，目标、信息流和资料空间打开同一 Resource。发布失败不重放模型工作，已完成回答仍可查看；内容不合规则产生关联原会话的待处理事项。
- 资料采用桌面内容／讨论双栏、手机内容／讨论切换。可明确保存回答，或把完整候选回答应用为原资料的新版本；CAS 防覆盖，旧版本保留。Work Product 不通过通用页面编辑绕过 TaskWorkspace。
- 资料支持空白页面、UTF-8 文本导入、收藏、收录／隐藏、集合和成员、列表／网格、服务端分页与搜索。导入限 100 KB 的 `.md/.txt/.json/.csv/.html/.htm`，拒绝非法编码与 NUL；HTML 作为内容读取，不执行其脚本。
- Goal／页面编辑具有离开前草稿保护；菜单筛选和滚动缓存、讨论输入草稿、对象深链继续使用现有 React 页面，不引入第二套 Router 或 Runtime。

搜索正文覆盖新笔记、已发布回答和后台结果；未建立正文索引的旧执行材料按标题检索，可从原会话核查。选区 AI 修改、块编辑、PDF／DOCX 解析、共享及站外投递不属于本轮能力。

## 2 后台研究的实际边界

保留旧 `workspace_snapshot` 的必选文件和零模型工具语义，新增不要求文件的 `research_readonly`。后者在 Host 中经父 Gateway 使用已配置、已授权的内建搜索／读取能力，先形成来源 receipt，再把来源交给原无工具后台模型。固定查询最多收集 5 条搜索摘要、3 个网页节选；搜索失败或无结果不会伪装成“今日无变化”。来源包含真实查询／URL、读取时间、覆盖或错误和 artifact 引用。

没有把 MCP、自定义或 Provider 原生联网偷偷提升为后台权限。确认型能力不能在无人值守工作中等待内存审批；当前内建外部检索需明确允许。现有 Hooks、SSRF、超时、取消和预算仍生效。用户当前工作区未配置内建搜索，界面明确显示不可用，没有替用户添加服务、切换模型或修改权限。

后台研究的联网请求契约用实际 Tavily 工具工厂及 HTTP MockTransport 验证，证明参数、来源处理与权限边界；本轮没有对真实外部搜索账户完成新闻简报 smoke，因此该项线上验收仍开放。Host 退出、休眠或关机时不工作。

## 3 可靠性、迁移与兼容

修复此前审查四项问题，并将反例纳入回归：启用时 queued admission 的 revision 竞争；仅因输入 hash 相同就吞掉新的成功结果；编辑丢失 `notify=false`；插件覆盖四个保留的助理工具名。

助理库 schema v1→v2 在事务前使用 SQLite backup 建立权限受限备份，再增加版本、关系、活动和运行 admission 记录。旧 ID、启用状态、历史 journal 保留；未知布局仍拒绝打开。Session v12／配置 v2 不变，不重写旧 journal。旧 CLI／API 继续投影到同一 Host／Scheduler。

关联对话先保留固定 Session identity，再创建 journal；重启重试能修复中断的创建。已经真实创建后被删除的会话不会被悄悄重建。关联运行持久化 request identity、固定引用和 digest；未知执行不会在重启时透明重放。成果发布与关系、信息流落在同一 Store 事务；模型结束与收录成功分别表达。

## 4 实际验证

| 验证 | 结果与证明范围 |
| --- | --- |
| `uv run --extra web --extra live pytest` | 最终 1570 passed、1 skipped；60 snapshots passed。包含 Host／API／Runtime／Loop／自动化及新增闭环、迁移、删除引用、发布失败和慢启动回归 |
| `uv run ruff check .` | 通过 |
| `uv run pyright` | 0 errors、0 warnings |
| `pnpm --dir src/web test` | 245 tests passed；覆盖 notify、目标意图、结构化讨论、草稿、发布 turn identity 和手机视图退出恢复 |
| `pnpm --dir src/web typecheck`／`build` | 通过，生成生产静态资源 |
| contracts／OpenAPI | contracts write／check 通过；OpenAPI 重新生成的文件 hash 与生成前一致。`api:schema:check` 因本次预期未提交 diff 返回 1，已检查差异，不能将该退出码写成干净 Git 检查通过 |
| Architecture Atlas | 更新手写 v2 图解与第 16 章，重建 content 并执行新鲜度检查 |
| `uv build`／隔离 wheel 安装 | 生成 `lumen_agent-2.0.0` wheel／sdist；Python 3.13 隔离环境安装，`lumen --version` 返回 2.0.0，`--check-config` 返回 Configuration OK；wheel 包含 Web 静态资源和 automation 模块 |
| 本地 8765 | 新后端 PID 18316，health 200，原目标与历史任务可见；助理库 schema 2，1 个旧目标与 76 个资料索引保留，发现 1 份 v1 备份 |

本机第一次后台启动实际已运行，但历史扫描超过旧 CLI 的 5 秒 readiness 窗口，导致 CLI 误报失败。再次启动给足等待后确认可用，随后将产品默认检测窗口改为 60 秒，并加入不实际等待的慢扫描回归；它只延长启动检测，不改变模型执行预算或权限。

真实 Provider smoke 使用现有 DeepSeek `deepseek-flash` 配置，在隔离工作区建立目标与关联笔记，实际运行模型并发布成果。终态为 `run.completed`，发布 1 份资源；回答包含笔记中的验收值 `CITRUS-742`，Session／interaction 身份匹配。该实验关闭工具、MCP、Hooks 和记忆学习，只证明资料上下文／真实模型／发布链，不代表全部 Provider、联网检索、成本或性能验收。

浏览器验收使用真实 FastAPI／Host／Runtime 和隔离的 FunctionModel 工作区，实际操作目标立即整理、资料关联讨论、完整候选应用为版本 2、再次讨论及无文件跟进草稿。测试模型只用于验证产品联动，截图中的“验收简报”不是真实新闻或搜索结果。

独立设计复核要求修复手机资料页退出后普通对话可能隐藏、资料成功提示残留到目标页两项问题；复核评分均为 resolved，disposition: ship，只覆盖这两项修复。相关证据为 [桌面目标](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/01-goal-desktop.jpg)、[资料讨论](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/02-resource-discussion-desktop.jpg)、[手机目标](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/06-goal-mobile.jpg)、[手机返回新对话](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/07-mobile-content-to-new-chat.jpg)、[手机返回历史对话](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/08-mobile-content-to-existing-chat.jpg)。完整 [verdict pass](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/verdict-pass.md) 与 [设计文档校验](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/documenter-report.md)保留其各自验证范围。

另在真实用户工作区的 8765 打开旧目标详情，确认新操作可见，没有为了截图创建或自动执行用户任务：[实际服务截图](../../.impeccable/review/lumen-v2-product-flow-2026-10-21/09-local-8765-goal.jpg)。隔离浏览器验收服务随后关闭，用户的 8765 保持运行。

## 5 发行限制

未执行目标平台远程 CI、真实移动设备、新 DMG 安装／签名／公证、长期定时运行、真实断电／磁盘满故障、全部真实 Provider 和真实外部检索账户验收。已有 MockTransport、快照、浏览器模拟尺寸及本地构建不能替代这些门禁。没有提交、推送或发布本次修改。

视觉继续沿用当前 Lumen 的中性色、Geist 与动态九尾狐，不把独立 Muse 演示原型当作官方功能契约，也没有复制其云端常驻能力。

## 6 会话状态与权限交互修复

后续排查确认：Host 在 finally 的成果保存／索引同步／释放运行状态之前发送 terminal，
Web 收到 terminal 后仅刷新一次 bootstrap，可能把旧 active run 留成“其他会话运行中”；
同时，其他会话结束没有当前 SSE 事件通知。现在 terminal 等待 Host 收尾，其他会话占用定期刷新。
收尾期间再次点击停止只等待已完成工作的保存，不取消保存或重复发布终态。

授权方面，项目规则原先只写文件，没有更新进程缓存；批量等待请求又在用户决定前一次性创建，
无法复用稍后授予的规则。现在写入成功后更新项目规则，尚未执行的匹配请求重新判定；
已打开会话和后续会话均可使用。运行中可调整审批模式，仍保留 Plan 只读、未知外部能力确认、
Git commit/push 等 confirm 风险每次确认，以及完整命令参数 digest 的记忆边界。

Web 授权卡片移至 composer 上方的有界滚动区，失败可重试，提交期间防重复，confirm 不展示记忆按钮。
模型可在运行中排队，结束后通过原 ResourceManager 原子替换 Runtime；取消排队与构建失败均有回归。
推理强度只改变下一次 Run，不改本轮实际 wire 请求；排队模型未切换时暂不允许设置新模型档位。
已保存回答按同一 interaction 的持久资源与 goal links 恢复，目标展示真实状态；操作位于回答之后。
额外修复重复发送、旧请求返回后覆盖新会话，以及终态后残留的待审批 UI。

交互参考 [ChatGPT 权限文档](https://learn.chatgpt.com/docs/permission-modes)中当前聊天的权限控制与
Sandbox／审批区分，以及 [ChatGPT Work 入门](https://learn.chatgpt.com/docs/get-started-with-work)中
运行过程可补充、审核与检查成果的方式。这里是 Lumen 的实现选择，不据此声称 ChatGPT 的模型热切换行为；
Lumen 的 auto 也不等同于 OpenAI 的自动风险审核。

本次验证：Python 全量 1,581 项（1,580 通过、1 项跳过），60 个 TUI 快照；Web 254 项测试、
typecheck 和生产构建；Ruff、严格 Pyright、contracts 与 Atlas 新鲜度检查通过。
api:schema:check 因已有未提交 schema 与 Git 不同返回 1，重新生成前后两个文件逐字节一致。
架构 Atlas 手写时序同步标明 Host 收尾后发布 terminal。

浏览器使用隔离 FastAPI／Host／FunctionModel 工作区，在 1280×720 与 390×844 检查长输出、
两项批量授权、运行中模型排队、一次会话授权释放整批、保存成果与刷新恢复。
证据：[桌面授权](../../.impeccable/review/conversation-permissions-2026-10-21/desktop-approval.png)、
[手机授权](../../.impeccable/review/conversation-permissions-2026-10-21/mobile-approval.png)、
[桌面完成态](../../.impeccable/review/conversation-permissions-2026-10-21/desktop-completed.png)、
[手机完成态](../../.impeccable/review/conversation-permissions-2026-10-21/mobile-completed.png)。
没有使用真实 Provider 调用或真实移动设备完成本次权限验收，没有重启用户现有服务或修改用户授权规则。

## 7 资料语义与输出界面修正

本节取代第 6 节中“保存回答”的产品选择。用户要求资料指向实际生成的文档、网页、图片等文件，
自动收录，不要求再次点击；既有笔记和历史回答资源保留，不删除旧数据。
用户提供的 ChatGPT 资料空间截图用于比较层级与交互，不作为运行指令。
参考 [ChatGPT Space](https://learn.chatgpt.com/docs/space) 的资料组织方式与
[文件查看器](https://learn.chatgpt.com/docs/artifacts-viewer) 的按格式阅读方式；没有引入未实现的共享或模板入口。

基线截图显示三个问题：保存回答替代真实文件、资料行缺少类型和来源层级、通用 Markdown 面板无法正确查看二进制与网页。
先捕获现有界面，再完成实现，进行一轮桌面／手机联合检查并修正级联样式、网格宽度、间距和手机预览标题。

1. 交付与收录：已修复。Host 收录已验证 Work Product 输出与本轮最终回答明确链接的新生成本地文件，保存实际 bytes、来源 interaction 和版本，自动关联当前目标的 output 关系；普通问答不变成资料。兼容显式页面发布与 revision CAS 保留。
2. 查找与浏览：已修复。资料空间改为搜索／创建、类型导航、收藏与视图控制；列表展示文件名、类型／大小、来源和时间，网格使用统一缩略图。列表、网格与类型过滤共享真实查询。
3. 打开与继续讨论：已修复。文件直接打开固定版本的格式查看器，可下载、查看详情或返回讨论；二进制讨论提供文件预览入口。HTML 禁止脚本和网络，明确关联资源使用当前工作区版本。
4. 对话输出：已修复。移除每轮重复身份与保存横幅，正文后显示去重的真实文件卡片，仅保留一处回答复制操作，缩小无意义留白；目标继续显示实际状态。

安全边界继续有效：descriptor／no-follow、工作区相对路径、敏感内容过滤、单文件 20 MiB、最多 32 条链接；
通用工具要求成功执行且文件 mtime 属于本轮，避免把旧输入文件收为产物。文件路径支持空格、编码及括号。
不会通过全盘扫描推断命令副作用，不绕过 Work Product 对账，也不会读取已删除会话的文件快照。

隔离 FastAPI／Host／FunctionModel 工作区实际生成 Markdown、HTML、CSV 与 PNG，通过真实 API 自动收录。
在 1280×720 与 390×844 核对列表／网格、图片、网页安全预览、文件名与工具栏、来源返回及刷新后成果卡片。
没有修改真实工作区的数据或重启用户 8765 服务；图像沿用仓库 fox-poster，无新增生成媒体。

验证：Python 全量 1,584 项（1,583 通过、1 跳过），60 个 TUI 快照；相关 Host／API／文件回归 92 项；
Web 257 项、typecheck、生产构建；Ruff、严格 Pyright、contracts／Atlas 检查通过。
api:schema:check 因未提交的预期 schema 差异返回 1，重新生成前后 OpenAPI 与 TypeScript schema 逐字节一致。
没有进行真实 Provider、物理手机、跨平台 CI 或大文件性能验收；Windows 通用文件收录保持 fail closed。

截图证据：
[基线资料空间](../../.impeccable/review/library-artifacts-2026-10-21/01-library-before.png)、
[基线对话](../../.impeccable/review/library-artifacts-2026-10-21/02-chat-before.png)、
[桌面列表](../../.impeccable/review/library-artifacts-2026-10-21/03-library-list-desktop.png)、
[桌面网格](../../.impeccable/review/library-artifacts-2026-10-21/04-library-grid-desktop.png)、
[文件版本预览](../../.impeccable/review/library-artifacts-2026-10-21/05-file-preview.png)、
[桌面对话](../../.impeccable/review/library-artifacts-2026-10-21/06-chat-desktop.png)、
[手机资料空间](../../.impeccable/review/library-artifacts-2026-10-21/07-library-mobile.png)、
[手机对话](../../.impeccable/review/library-artifacts-2026-10-21/08-chat-mobile.png)、
[手机文件预览](../../.impeccable/review/library-artifacts-2026-10-21/09-file-preview-mobile.png)。

## 8 目标与资料管理、旧数据清理

用户明确要求清理旧资料数据，并补充新增／修改／删除能力。本轮保留既有视觉系统与文件收录语义，
使用行尾菜单管理对象；目标增加搜索、状态筛选、编辑、完成、归档、删除和恢复，新建后打开详情。
资料支持导入原文件（20 MiB）、重命名、页面内容编辑、目标／集合关联、删除和恢复；集合支持改名与删除。
删除集合只解除分组。重命名不改工作区文件名，文件内容修改继续经过原对话与 TaskWorkspace 校验。

Goal／Resource 使用可选 deleted_at 与 revision CAS，旧数据可加载且不改 Session schema。
默认查询与目标详情过滤回收站，恢复保留原内容、收藏、集合及目标关联。历史快照读取同样检查当前删除状态。
同步 Work Product 或重新收录同路径文件时保留墓碑与显示名称，避免清理后又复活。
删除目标与 Run admission 共用 Host state lock，等待 Host 收尾结束；模型已完成但尚在收录文件的窗口也不能删除目标。
启用的持续跟进与 queued／running／settling 后台执行同样阻止删除，必须先暂停和等待。
上传经过 cookie／Origin 校验、流式大小限制、Host 文件名／敏感内容校验及 UUID 身份幂等；原文件 bytes 存 ArtifactStore，不写入工作区。

真实工作区维护前取得运行锁，确认无执行占用，停止旧服务并使用 SQLite backup 保存受限权限备份：
`.lumen/sessions/assistant/dfbdbc3866691fe0b4040c18/state.before-management-20261021T054334Z.sqlite3.bak`。
通过 Store 的管理操作将 85 条旧资料、1 个旧目标移入回收站，正常列表清零；当时没有集合或持续跟进定义。
清理过程核对 66 份会话 journal 的 SHA-256 未变，没有删除工作区原文件或 Artifact。SQLite integrity_check 为 ok。
新版服务已重新启动，重建索引后仍无旧资料回到正常列表；维护摘要保存在 `.lumen/assistant-cleanup-2026-10-21.json`。
这是可通过产品回收站恢复的清理，没有执行不可逆的文件擦除。

验证：Python 全量 1,587 项（1,586 通过、1 项跳过），60 个 TUI 快照；Web 261 项、typecheck、生产构建；
Ruff、严格 Pyright、contracts／Atlas 检查。OpenAPI 重新生成前后逐字节一致，api:schema:check 因预期未提交 diff 返回 1。
新回归覆盖认证／Origin、导入幂等和大小／路径、陈旧 revision、删除／恢复、关联保留、原文件编辑保护、
Work Product 重索引不复活、执行与收尾窗口的目标删除保护，以及 UI 删除确认、失败重试、集合删除和导入。

隔离 Host 浏览器在 1280×720 与 390×844 完成目标编辑、资料重命名、删除和回收站恢复；菜单复用既有焦点与键盘机制。
一轮联合检查修正菜单文字竖排及目标列表留白后，进行最终确认。独立 finish reviewer 给出 ship（仅本轮管理交互的视觉范围），
documenter 核对 PRODUCT／DESIGN 与源码一致，无需另改设计系统。未进行真实 Provider、物理手机、读屏器或跨平台 CI 验收。

截图：
[桌面目标](../../.impeccable/review/goal-library-management-2026-10-21/01-goals-desktop.png)、
[桌面资料](../../.impeccable/review/goal-library-management-2026-10-21/02-library-desktop.png)、
[手机目标](../../.impeccable/review/goal-library-management-2026-10-21/03-goals-mobile.png)、
[手机资料](../../.impeccable/review/goal-library-management-2026-10-21/04-library-mobile.png)、
[回收站](../../.impeccable/review/goal-library-management-2026-10-21/05-recycle-mobile.png)、
[真实资料清理后](../../.impeccable/review/goal-library-management-2026-10-21/06-live-library-cleaned.png)、
[真实目标清理后](../../.impeccable/review/goal-library-management-2026-10-21/07-live-goals-cleaned.png)。

## 9 Web 刷新认证失效与连接恢复

用户在 Chrome 的 `127.0.0.1:8765` 刷新后看到 `authentication required`、空历史和未配置模型。
这属于浏览器认证失效，并非模型或任务执行失败。根因有两处：`create_web_app` 每次启动随机更换
session secret，重启服务后旧 Cookie 失效；原固定 Cookie 名称又不按端口隔离，同一主机上的
其他 Lumen 工作区／测试后台可能覆盖它。上一阶段服务维护和隔离浏览器登录无法证明用户 Chrome 可刷新，
本节补充原浏览器上的实际验收。

CLI 现在通过 `api/auth.py` 在工作区内按监听地址保存随机密钥，私有文件不进 Session、配置或日志。
POSIX 检查所有者、私有权限、常规文件类型和有界内容，descriptor/no-follow 拒绝读取路径上的符号链接；
创建复用 Workspace 的原子写入与并发冲突检查。损坏或公开权限文件明确报错，不覆盖已有密钥。
Cookie 名称由密钥摘要隔离，HTTP 与 Live WebSocket 使用相同认证；桌面私有 IPC 后台仍使用进程级随机密钥。
启动 token 继续一次性兑换，入口移除 token、保留导航参数且不缓存；过期链接进入恢复页，不签发 Cookie。

前端集中识别 API 401，展示中文“重新连接 Lumen”，停用对话输入区并保留当前草稿。
初始化网络故障显示单独连接错误和重试；不在 timeline 追加失败回答，也不将 401 转成任务失败。
重新连接只读取 bootstrap／历史／选中会话并恢复事件订阅，不自动重试写操作或重发任务。
清除 Cookie、更换浏览器或首次修复升级仍需要新的启动链接；旧进程已丢失的认证密钥无法还原。

实际服务更新前取得工作区执行锁，确认没有占用后停止服务并部署生产 Web 导出。
已在用户原 Chrome 标签页重新登录一次：历史会话与 deepseek-flash 恢复，普通刷新通过；
随后再次重启服务、不打开新启动链接，原 Chrome 刷新仍可读取历史并使用模型，地址栏无 token。
一次性验证链接未进入文档或日志，私有临时交接文件验证后删除。本轮没有清理或改写目标、资料或会话记录。

验证：相关 Python 108 项（认证文件、CLI、API、Host、桌面）通过；Web 267 项、typecheck 与生产构建通过；
Ruff、严格 Pyright、contracts 检查通过。OpenAPI／TypeScript schema 重新生成前后逐字节一致，
api:schema:check 因既有未提交 schema 差异返回 1。Atlas 文本与手写客户端能力说明同步更新并重建。
新增回归覆盖跨工作区／地址密钥隔离、权限与损坏拒绝、重启 Cookie 复用、跨后台 Cookie 不碰撞、
过期链接无授权、Host／Origin 限制、401 分类、无写操作重放、重连保留草稿和网络错误恢复。

1920×848 原 Chrome 与 390×844 浏览器宽度完成联合视觉检查。独立 finish reviewer 的结论为
**ship**，仅覆盖新增连接恢复界面；documenter 确认是既有视觉系统扩展，保留 DESIGN.md 与 sidecar。
detector 的两项 primary 命中既有渐变文字和宽度动画，不在本次新增恢复区域内。
本轮没有重跑 Python 全量、真实 Provider、物理手机或 Windows／Linux 实机验收。

截图证据：
[桌面登录恢复](../../.impeccable/review/web-auth-recovery-2026-10-21/01-reconnect-desktop.png)、
[手机登录恢复](../../.impeccable/review/web-auth-recovery-2026-10-21/02-reconnect-mobile.png)、
[Chrome 登录后刷新](../../.impeccable/review/web-auth-recovery-2026-10-21/03-chrome-refresh-restored.png)、
[服务重启后 Chrome 再刷新](../../.impeccable/review/web-auth-recovery-2026-10-21/04-chrome-after-server-restart.png)。

## 10 信息流与回收站关联修复

用户再次报告信息流仍有旧数据。只读检查真实数据库确认：85 条资料与 1 个目标已标记 deleted_at，
但 feed 中两条旧发布通知仍被正常查询返回；右侧目标栏也直接渲染 overview 中为回收站保留的目标。
因此此前清理动作已执行，遗漏的是关联投影的可见性规则，而非再次执行不可逆删除。

Store 的直接 feed 读取、分页查询与 snapshot 现在共用关联条件：条目引用的每个目标／资料必须存在且未删除。
SQL 在 LIMIT 前过滤，覆盖 all／attention／unread、搜索和游标分页；不关联对象的正常待处理通知不受影响。
原 feed 记录、sequence、已读状态和对象关系保留，全部关联对象恢复后通知重新可见。
Web 目标侧栏过滤墓碑并显示空状态；Engine 不再为已删除、已完成或已归档目标生成推进灵感。
自动化新结果只关联未删除目标，避免独立执行继续沿用旧关联而被错误隐藏。

验证覆盖单独删除目标／资料、两者同时删除后部分恢复仍隐藏、全部恢复重新显示、重启后状态、
缺失引用、被隐藏记录不占分页位置、未读与待处理筛选、API／overview 一致性，以及回收站仍可恢复。
新增测试还核对删除目标后的新自动化结果不再关联旧目标，已删除目标不进入灵感与信息流侧栏。
相关助理回归 40 项、Host／API／管理回归 84 项通过。最终 Python 全量 1,594 通过、1 项跳过，
60 个 TUI 快照通过；Web 268 项、typecheck、生产构建、Ruff、严格 Pyright 和 contracts 检查通过。
没有更改 API schema、Session schema 或助理数据库表结构；架构图谱重新生成并检查。

更新真实服务前取得工作区执行锁。重启后通过认证 API 核对三个信息流筛选均为空，正常资料和目标均为 0；
用户原 Chrome 刷新后旧卡片和右侧旧目标消失，待处理／未读筛选也为空，登录未失效。
维护前后 feed 的原文与顺序摘要一致，仍保存 2 条历史通知；回收站仍为 85 条资料、1 个目标。
只读核对摘要保存在 `.lumen/feed-cleanup-2026-10-21.json`；本轮未重新删除资料、目标、原文件或会话。
没有进行真实 Provider、跨平台实机、长期调度或性能验收。

截图：[修复前](../../.impeccable/review/feed-cleanup-2026-10-21/01-before.png)、
[服务更新并刷新后](../../.impeccable/review/feed-cleanup-2026-10-21/02-after-restart.png)。

## 11 目标功能边界与创建／编辑界面

用户截图中的新建表单仍把里程碑和自动化选择器混在一起，空 fieldset 的 legend 又形成突兀边框。
代码还发现目标详情下重复渲染列表卡片；里程碑用文本行号复用 ID、按标题推断完成状态，删除首项或重命名会错位。
“现在整理一次”快捷入口同时执行并要求发布回答，与成果文件自动收录的现行语义冲突。

新建目标现在只收集标题和可选说明，保存后打开详情，不创建会话、运行或调度。目标是持久意图及用户确认的结果，
与单轮 Run Plan、调度定义分别管理。“开始规划／继续讨论”复用固定关联对话，只准备草稿，用户发送后才执行。
移除自动执行／发布整段回答的快捷路径；Host 的显式发布兼容接口与已有页面应用能力保留。

详情分为里程碑、真实成果文件和参考资料；持续跟进单独收在默认折叠的可选区域，旧安排仍可管理。
目标编辑逐项维护里程碑的稳定 ID、顺序和 done；新增分配新 ID，删除不会把状态转移到其他项。
基础编辑原样保留已有 automation_ids，不启用或暂停安排。新建不要求预写计划，完成仍由用户确认。
overview 轮询更新详情，编辑草稿保持独立；导航后忽略旧请求响应，避免旧目标覆盖新页面。
长表单只有字段区滚动，底部保存保持可见；未保存内容有关闭保护，提交中禁止重复保存和关闭。

本轮验证：Web 273 项、typecheck、生产构建通过；助理流程／管理／Host／API 回归 97 项和 contracts 检查通过。
新增组件回归覆盖稳定 ID、完成状态、既有安排保留、空标题、提交保护、轮询草稿及过期响应。
隔离的真实 Host／FunctionModel 环境完成浏览器保存：删除首项、重命名和新增后只读核对持久 ID／done；
新建目标没有 Session／run／自动化，点击规划才建立固定 Session，仍无 admission，输入区显示待发送草稿。
验证数据只存在临时工作区，不保存到用户真实资料、目标或会话。

1280×800 与 390×844 完成联合视觉检查；一次修正编辑区滚动和底部保存后确认。
独立 finish reviewer 给出 **ship**，detector 为 0 primary、18 项既有／记录内 advisory；documenter 确认
PRODUCT 与实现一致，保留原 DESIGN.md 和 sidecar，没有引入新的视觉系统。当前 docs／PRODUCT 已同步。
取得工作区执行锁后更新并重启 `127.0.0.1:8765`。新 Chrome 标签页沿用已有 Cookie，实际确认新版新建表单
只含两个字段、没有自动化选择器；未重载用户原对话，也未在真实工作区保存测试目标。隔离服务和临时数据验证后清理。
本轮仅改 Web 生产代码，没有更改 API／数据库／Session schema，未重跑 Python 全量或真实 Provider、物理手机、跨平台实机验收。

截图：[桌面新建](../../.impeccable/review/goal-flow-2026-10-21/01-new-desktop.png)、
[手机新建](../../.impeccable/review/goal-flow-2026-10-21/02-new-mobile.png)、
[手机详情](../../.impeccable/review/goal-flow-2026-10-21/03-detail-mobile.png)、
[桌面详情](../../.impeccable/review/goal-flow-2026-10-21/04-detail-desktop.png)、
[桌面编辑](../../.impeccable/review/goal-flow-2026-10-21/05-edit-desktop.png)、
[手机编辑](../../.impeccable/review/goal-flow-2026-10-21/06-edit-mobile.png)、
[真实服务新建表单](../../.impeccable/review/goal-flow-2026-10-21/07-live-new-goal.png)、
[规划仅准备草稿](../../.impeccable/review/goal-flow-2026-10-21/08-planning-draft.png)。

## 12 目标原对话关联、显式推进与收尾

用户接受对 Codex／Muse 的研究判断后，要求完成逻辑优化。本轮将已确认的缺口落实为共享 Host 契约，
保留第 11 节的意图式创建，不把创建改成运行。参考结论是产品语义比较，不声称复现闭源产品内部实现。

- 从对话“设为目标”传递 source_session_id，经 Host 检查所属工作区、可用性和归档状态后绑定原 Session。
  保留原约束和 journal，discussion 多个引用并存，兼容旧单 context 记录；删除目标不阻断原普通对话。
- Goal 业务状态显示未完成／已完成／已归档，execution 单独记录成功标准、可选总轮数、实际推进和核验。
  页面不沿用上一段聊天的运行提示或检查点控件。创建和讨论草稿仍不执行，只有明确启动／恢复才推进。
- ControlGoal／GoalCommand 复用原 StartRun、RunCoordinator、Runtime、工具、审批、沙箱和完成门禁。
  Host 等待本轮持久化、收录和释放席位后才尝试下一轮；不增加另一 Runtime 或调度器。
  控制请求幂等、revision CAS、轮数累计；预算用完停止，新消息、无工具进展、报告缺失或故障不续跑。
- 根推进 run 的 report_goal_progress 只能引用本轮真实成功工具回执，拒绝空的 complete 证据、伪造 ID、
  错误或非零退出码；报告后新工具或新消息使旧候选失效。只有原单轮完成门禁通过后才采纳。
  review 是提交用户验收，明确完成才 accepted；没有把模型自评或一次工具成功当成业务达成证明。
- 暂停先持久化再取消，保留原工作；重启后暂停遗留 active，不自动重放。
  完成／归档且有启用跟进时必须选择 pause 或 keep；暂停事务内取消 queued，不把运行中任务当作已经停止。
  Goal／配置／Session 旧记录仍可读，无 SQLite 表迁移、无旧 journal 重写；生成契约和 OpenAPI 已更新。

最终验证：`uv run --extra web --extra live pytest -rs` 为 **1605 passed、1 skipped**，60 个 TUI 快照通过；
唯一跳过是需 LUMEN_LIVE_BROWSER=1 和 crawl4ai 环境的真实联网浏览器测试。
桌面私有后台集成另跑 9 项通过。Web **278 项**、typecheck 和生产构建通过；Ruff、严格 Pyright、
contracts 检查通过。OpenAPI／TypeScript schema 重新生成前后哈希一致；api:schema:check 因已存在的未提交
schema 差异返回 1，已核对新增 ControlGoal／GoalCommand／GoalExecution 与输入字段为预期变更。

新增 11 项 Python 用例覆盖跨轮持久化后续跑、预算不续租、无进展／空或伪造证据停止、原 Session 及多引用保留、
删除目标后原对话仍可执行、未登记来源拒绝、完成／归档的暂停与保留选择、queued 取消、暂停与重启、认证／Origin／Plan 门禁。
新增 5 项 Web 用例覆盖原对话创建关联、不隐式执行、成功标准必填、预算与暂停、收尾明确选择和目标页去除聊天控件。

隔离真实 Host／FunctionModel 的 1280×800 与 390×844 浏览器验收：从未启动目标明确启动，实际进入第二轮；
原对话仍显示原约束和推进记录；对话内暂停后执行终止；完成并暂停跟进后显示已完成／已验收及 0 项启用。
首轮联合检查修正聊天状态和控件串入目标页的问题，第二轮确认；截图尺寸均检查并打开核对。
detector 只运行一次，0 primary、18 项既有／记录内 advisory。界面沿用既有 Geist、中性色和扁平分节。
独立 finish reviewer 检查全部 9 张最终截图后给出 **ship**，范围为本轮目标推进与收尾界面；
documenter 核对成品、PRODUCT／DESIGN 与 sidecar，确认是既有设计系统扩展，保留原设计文件。
取得工作区执行锁并确认没有活动任务后，更新并重启 `127.0.0.1:8765`。新 Chrome 标签页沿用已有 Cookie，
实际确认历史任务、目标列表和新版意图式新建表单正常加载，没有 authentication required；
未重载用户原对话、未保存真实测试目标、未调用真实 Provider。隔离验证服务和临时工作区在验收后清理。
这不代表真实 Provider 的进度报告稳定性、长期无人值守、费用、物理手机、读屏器或 Windows／Linux 实机验收。
轮数上限不等于 token 或费用上限；核验真实性校验不等于形式化证明成功标准。

截图：[桌面详情](../../.impeccable/review/goal-execution-2026-10-21/01-detail-desktop.png)、
[桌面启动](../../.impeccable/review/goal-execution-2026-10-21/02-start-desktop.png)、
[手机启动](../../.impeccable/review/goal-execution-2026-10-21/03-start-mobile.png)、
[手机推进](../../.impeccable/review/goal-execution-2026-10-21/04-active-mobile.png)、
[桌面推进](../../.impeccable/review/goal-execution-2026-10-21/05-active-desktop.png)、
[对话暂停](../../.impeccable/review/goal-execution-2026-10-21/06-paused-conversation.png)、
[桌面收尾](../../.impeccable/review/goal-execution-2026-10-21/07-close-desktop.png)、
[手机收尾](../../.impeccable/review/goal-execution-2026-10-21/08-close-mobile.png)、
[完成验收](../../.impeccable/review/goal-execution-2026-10-21/09-completed-mobile.png)、
[更新后的真实服务](../../.impeccable/review/goal-execution-2026-10-21/10-live-create.png)。

## 13 模型／思考强度组件与可执行灵感

重新读取 [Muse 官方设计文章](https://introducing.muse.ai/)的 Finding the magic：Ideas 的价值来自目标、
对话和已知上下文中的有用建议，承接后续行动；原文没有公开推荐算法或“点击建议立即执行”的内部契约。
因此以下为 Lumen 的产品设计推导：建议要说清依据、下一步和成果，通用模板不能冒充个性化理解，
查看不执行，明确的开始按钮才提交任务。没有复制 Muse 的云端常驻、跨应用观察或后台自主授权。

Codex 组件以用户提供的截图为视觉参考。模型与 effort 的真实能力继续来自已有 Provider 目录与
ReasoningSelection，不用截图中的固定档位覆盖供应商事实。
[OpenAI 官方 reasoning 文档](https://developers.openai.com/api/docs/guides/reasoning)说明 effort 指导思考量，
支持项和默认值依模型而异；不能把低／高档位解释成固定时延或质量保证。

- 模型列表改成有搜索和已选状态的紧凑浮层，按视口限制宽度；运行中模型仍在本轮结束后切换。
  思考强度使用当前模型、默认恢复和离散滑块面板；拖动预览，松开或选择档位后保存一次。
  别名映射可见，默认档位未知时不伪造滑块位置；Session 设置仍用于下一轮，不改变审批或 Provider 参数映射。
- Engine 从未完成目标与可用文档生成有依据的建议，包含来源、步骤、预期成果及版本引用。
  正在推进的目标不再建议重复启动；待验收目标打开详情，未验证的跟进安排打开原配置。
  保留三个明确标注的通用起点，要求填写文件、时间或目标等任务范围；没有读取私人习惯或暗中调用模型。
- 内联方案取代“开始聊聊”。明确“开始任务”调用既有 discussion／StartRun：目标和资料复用固定关联
  Session 及全部已有引用，普通模板建立普通 Session。启动前刷新核对建议，Host 继续核对引用版本、
  对象可用性、忙碌、审批和沙箱。失败保留输入及同一请求身份，迟到响应不抢回用户已经离开的页面。
  文件使用原有自动收录与目标关联；不会把这次普通任务当成已启动持续目标推进，不替用户验收或启用调度。
- 没有修改 Runtime、Loop、Coordinator、审批或 Session schema；Idea 新字段有默认值，兼容旧记录。
  删除目标后不再推荐该目标；仍可用的独立资料可以继续产生建议，不因目标删除而一并抹除。

验证：Python 全量 **1610 passed、1 skipped**，60 个 TUI 快照通过；唯一跳过为显式启用才运行的联网浏览器测试。
相关建议／助理／目标推进／Host／API／reasoning 回归 220 项通过。Web **285 项**、typecheck 和生产构建通过，
Ruff、严格 Pyright、生成契约检查同步。新增用例覆盖建议状态与版本、忽略、删除、资料范围、明确启动、
关联 Session 的全部引用、启动失败复用请求身份、撤回建议拒绝、迟到响应不抢页面、滑块预览／提交／失败／默认。

隔离 WorkspaceHost＋FunctionModel 浏览器验证覆盖 1280×800 与 390×844；实际开始一项关联目标的建议，
在原 Session 执行、产生文件并自动收录。该环境没有调用真实 Provider，截图中的模型名称明确标注隔离验证；
FunctionModel 不验证真实模型的规划、来源读取质量或工具使用稳定性。首轮检查发现并修正字段样式、滑块圆柄
颜色和手机底栏空间问题；最终独立界面复核另记于交付记录。

独立 finish reviewer 的初次结论为 fix：390px 的模型名称被压缩；第一轮修正解除重叠但仍只显示 G…，
复核判定 partial。第二轮改为 ≤480px 设置独立一行，重新捕获相同桌面／手机状态及关闭面板后的发送控件，
reviewer 对唯一 F1 判定 **resolved／ship**；这是对指定修复项的验收，不外推为完整读屏器或实机验收。
documenter 确认 PRODUCT 一致，并修正本轮造成的 DESIGN 局部布局文字与架构描述过宽问题；
全局视觉 token 和 sidecar 保留。detector 只运行一次：3 项 primary 为既有 shimmer、侧栏宽度动画和代码字体，
290 项 advisory 包括旧样式及局部尺寸；本轮没有因此扩展为全站视觉系统改造。

取得工作区执行锁并确认没有活动任务后，更新并重启本机 8765。新 Chrome 标签页沿用 Cookie，
真实确认新版灵感与原历史任务正常加载，通用方案在范围为空时禁止开始；未在真实工作区提交测试任务或调用模型。
真实服务以可见界面及 AX 状态核对；本轮原生 Chrome 截图返回的保存图像不完整，已弃用该文件，以下仅保留有效隔离截图。
隔离服务和临时文件在验收后清理。OpenAPI／TypeScript schema 重新生成前后哈希一致，Atlas 和 contracts 新鲜度检查通过。

截图：[桌面方案](../../.impeccable/review/models-ideas-2026-10-21/01-ideas-desktop.png)、
[手机方案](../../.impeccable/review/models-ideas-2026-10-21/02-ideas-mobile.png)、
[手机强度](../../.impeccable/review/models-ideas-2026-10-21/03-thinking-mobile.png)、
[桌面强度](../../.impeccable/review/models-ideas-2026-10-21/04-thinking-desktop.png)、
[桌面模型](../../.impeccable/review/models-ideas-2026-10-21/05-model-desktop.png)、
[手机模型](../../.impeccable/review/models-ideas-2026-10-21/06-model-mobile.png)、
[实际任务与文件](../../.impeccable/review/models-ideas-2026-10-21/07-idea-result-desktop.png)、
[手机独立控件行](../../.impeccable/review/models-ideas-2026-10-21/08-mobile-composer.png)。

## 14 模型与思考统一组件

用户再次指出模型与思考仍是两个入口，强度浮层重复显示模型。第 13 节的独立控件与手机两行布局
由本节替代：二者是同一配置流程，应共享入口与面板。权限仍为独立决策。

- 底栏只保留一个“模型 · 强度”按钮。统一 `ModelSettingsControl` 拥有 settings／models 两个内部
  视图与一个 portal；打开后直接调整强度，点击顶部模型进入搜索，选好并刷新能力后返回同一面板。
- 删除独立 ReasoningControl 与第二入口状态；通用 ChoiceMenu 中原模型专属搜索和 composer 对齐
  分支已无生产调用，随之移除。权限及语音的原通用选项逻辑保留。
- 原 Host／Session 设置 Interface 不变：模型运行中排队，思考用于下一轮。待切换状态不展示旧模型
  档位；不支持强度调节仍可选模型。别名、未知默认、恢复默认、保存失败重试和输入草稿保持原语义。
  关闭面板时未完成的模型请求不会在返回后重新打开它，重叠保存受同一 pending 状态保护。
- 桌面显示模型、默认及实际默认档位；390px 手机隐藏入口图标及默认档位后缀，长名称截断，
  在面板中查看完整名称；模型、强度、权限与添加／发送保持同一行。没有新增依赖或视觉 token。

验证：Web **293 项**、typecheck、生产构建通过。新增／更新回归覆盖统一入口与同一面板切换、
模型改变后能力刷新、无强度能力、排队模型、模型保存错误、关闭期间异步响应及原滑块提交契约。
本轮仅改 Web，没有改动 Python／schema／Provider 参数，也未重复运行 Python 全量。

隔离 WorkspaceHost＋FunctionModel 验证 1280×800、390×844：从 deepseek-flash 选择 GPT-5.6 Sol，
同一面板从四档刷新为六档，再保存高强度；只产生一个空 Session，未提交任务或调用真实模型。
六张截图均检查尺寸并逐张打开确认有效。单次 detector 的既有 shimmer、侧栏宽度动画、代码字体及
incumbent 样式建议没有扩展为无关视觉系统改造。

独立 finish reviewer 首次指出 Tab 边界包含不可顺序聚焦的模型选项、窗口级 Escape 未避开输入法
候选状态、搜索占位文字对比度不足；同一批修正并增加三项回归。实际浏览器确认 Tab 离开搜索后
关闭并返回统一入口，方向键进入选项后 Shift+Tab 返回搜索。重新捕获六个相同状态后，原 reviewer
对 F1–F3 全部判定 **resolved／ship**，此 verdict 只覆盖这三项，不扩展为全站审计。documenter
确认 PRODUCT／DESIGN 与最终组件一致，保留既有 token 与 sidecar。真实手机与真实输入法未验收。

确认执行锁可取得后更新本机 8765；新 Chrome 标签页沿用 Cookie，真实工作区历史正常加载，
统一入口打开同一模型／强度面板。未改动真实工作区的模型、思考或权限，未提交测试任务。
真实服务使用 AX 核对；截图均来自上述隔离环境。隔离服务、临时文件及本轮验证标签页验收后清理。

截图：[桌面统一设置](../../.impeccable/review/combined-model-settings-2026-10-21/01-settings-desktop.png)、
[手机统一设置](../../.impeccable/review/combined-model-settings-2026-10-21/02-settings-mobile.png)、
[手机模型搜索](../../.impeccable/review/combined-model-settings-2026-10-21/03-models-mobile.png)、
[桌面模型搜索](../../.impeccable/review/combined-model-settings-2026-10-21/04-models-desktop.png)、
[切换后原面板](../../.impeccable/review/combined-model-settings-2026-10-21/05-switched-settings.png)、
[手机单行入口](../../.impeccable/review/combined-model-settings-2026-10-21/06-composer-mobile.png)。
