# 计划与产物展示复审

## 结论

上轮将“可恢复的计划状态”和“常驻计划入口”混在了一起。页头入口、右侧抽屉、历史过程清单和 composer 胶囊同时存在，造成重复；胶囊缺少 active run 条件，使完成任务仍占用输入区。此问题应修正 Web 投影，而不是删除 Session 中的计划事实。

当前策略：**执行进度是临时 UI，计划事实是持久记录，文件是可再次访问的产物。**

| 数据 | 保留原因 | 展示位置 |
| --- | --- | --- |
| Plan revision / state_version、步骤和证据 | 恢复、审核版本校验、完成门禁与审计 | 当前运行胶囊、Plan Mode 待审正文；历史仅在运行记录 |
| 运行中的步骤进度 | 回答“现在在做什么” | 输入框上方居中，terminal 后卸载 |
| 生成或修改的文件 | 完成后仍需阅读、复用与下载 | 对话文件卡片及文档预览 |

一个 Session 的最新计划仍由 Coordinator/Runtime 管理；新 turn 未产生计划时，不把前一轮当成新任务进度。不能在 UI 关闭时清空领域状态，否则停止后继续、重启恢复与审核 revision 都会受影响。`/tasks` 作为兼容命令打开运行记录，不再维护单独计划抽屉。

## 本轮变更与删除证据

- 删除页头计划按钮、`planOpen`、`PlanDrawer`、对应 CSS。生产入口已由运行胶囊、待审方案和既有运行记录覆盖，零剩余生产引用。
- 历史过程不再投影 plan；保留原始 timeline/journal 数据。更新原先“历史过程显示计划”的测试，保留不可改写记录的断言。
- 删除 `PlanPanel` 的折叠分支和样式；当前两个生产调用者都是完整清单，分支已不可达。
- 胶囊添加 active run 条件并居中。测试覆盖 running → completed 后立即消失、已完成任务无入口、新 turn 不沿用旧计划、审核与键盘操作。

## 文档能力：此前未闭环，现在已实现基础预览

原实现只有回复 Markdown 渲染和输入用文件搜索，没有文件内容读取 API。现在的链路是：

`本轮成功文件写入 / 回复文件链接 → 用户点击 → 认证 API → WorkspaceHost.read_document → 有界文件读取 → 预览 / 下载`

- 卡片从本轮成功的 `write_file` / `edit_file` 参数投影并去重，不宣称捕获任意 shell 或插件的所有文件副作用。模型最终回复中的本地 Markdown 链接、内联文件路径也可以打开；链接本身不证明文件存在，失败进入可重试状态。
- 支持 Markdown 阅读与源码切换、HTML 静态预览、文本/代码、PNG/JPEG/WebP/GIF 和浏览器 PDF 查看。SVG 按源码显示；Office 文件尚无内嵌渲染。文本超过 1 MiB 只下载，读取总上限 20 MiB。
- 当前文件读取使用已有 workspace 解析，并逐段通过 directory descriptor 与 `O_NOFOLLOW` 打开；拒绝隐藏/父级路径、绝对路径、符号链接与特殊文件。读取放入线程，阻止大文件与 FIFO 等无限占用。
- Markdown 预览中的相对文档链接按当前文档目录解析；向上引用先归一化，不能越过工作区。API 仍只接受归一化后的工作区相对路径。
- API 保留登录 cookie/Host 校验，直接导航固定 attachment/octet-stream、no-store、nosniff 与 CSP，不在应用同源直接执行 HTML。
- HTML 使用空 sandbox iframe；CSP 禁止脚本与网络资源，移除 meta refresh/base/外部超链接，阻止自动导航逃离静态预览。静态布局可读，交互脚本不运行。关闭时 abort 请求并释放 Blob URL，迟到结果不会重开窗口。
- 预览明确标记“当前文件”。它不是当时交付的不可变快照，不另建文件索引或复制 TaskWorkspace 的状态权威。

## 验证与实际限制

- 960 项 Python 测试、60 个 snapshot 通过；相关 Host/API/文件读取测试 51 项通过。
- 127 项 Web 测试通过；Ruff、Pyright、TypeScript、契约和 Atlas 检查通过；OpenAPI 与 TS schema 经命令生成，Next production build 通过。
- 使用独立临时工作区与 8772 测试 Host，没有调用外部模型或重启用户 8765 服务。运行/待审生命周期由测试 Host 提供，文档内容读取使用真实新 API、Host 与磁盘文件。
- Chrome 验证已完成页面无计划胶囊/抽屉；running 胶囊中心与输入框中心同为 x=1090；展开与关闭正常。Plan Mode 修改入口聚焦意见框，保留原审核流程。
- Chrome 验证 Markdown 标题/表格、源码切换、下载入口；HTML 仅显示静态内容，fixture 的 script 未执行。截图保留在本任务工具记录，未保存为本地图片。不把自动化中的下载链接检查描述为已核验下载落盘。
- 没有宣称与 ChatGPT 完整文件平台等价：Office 内嵌、不可变历史快照切换、HTML 脚本执行和超大文档尚不支持。
- 已运行的 Lumen 后端进程需要重启以注册新 `/api/v1/files/content` 路由，然后刷新页面；本轮未中断用户原服务。

本轮实现不以打开之前被浏览器拒绝的本地 HTML 原型为验证手段。该原型的像素对照限制仍保留；当前验证针对用户最新要求的展示规则与文档交互。
