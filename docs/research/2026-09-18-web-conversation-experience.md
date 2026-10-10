# Web 交互演进与验收记录

整理：2026-10-10；原始研究与验收：2026-09-03 至 2026-09-18。本文合并过程展示、消息操作、
侧栏、异步标题、Plan UI、文件预览及根目录设计 QA 的阶段记录。测试数量属于各次原始运行，
不代表本次整理重跑测试；现行行为以[命令说明](../commands.md)、源码和契约测试为准。

## 已采纳的交互与修订

| 范围 | 合并后的结论与依据 |
| --- | --- |
| 过程展示 | 执行时按公开事件顺序展开文字和工具；使用页面自然滚动，消除小字号和嵌套限高滚动。成功后收起，失败或停止保留展开；待澄清的问题使用独立回答卡片。耗时是整个 turn 的墙钟时间，不冒充纯模型计算时间 |
| 后续修订 | 9 月 18 日首版曾默认折叠过程、单独折叠 thinking、合并连续成功调用；随后用户反馈撤销这三项。运行中手动展开不阻止成功结算时收起；结束后用户主动展开可继续阅读 |
| 真实状态 | 只有实际活动的思考或工具显示低对比流光；审批等待时暂停。文字、工具结果来自公开事件，不推断隐藏思考，不伪造阶段或进度；reduced-motion 与 forced-colors 使用静态标记 |
| 消息操作 | 停止使用圆形按钮；编辑时保留附件与草稿，支持中文输入法。9 月 3 日编辑曾创建新 Session 分支，9 月 10 日已改为同 Session 的活动历史重生成，详见[重生成审计](2026-09-10-in-place-message-regeneration.md)；原始 journal 和未决副作用门禁保留 |
| 标签与协议 | 普通阅读去掉显示层控制标签，代码样例与详细原文保留；不改写 canonical 消息、签名 thinking 或 Provider 恢复正文。协议和输出目录的研究取舍见[专项审校](2026-09-04-thinking-plan-output-review.md) |
| 滚动与标题 | 原生滚动延伸至页边，正文保留阅读栏；桌面消息导航支持方向键/Home/End，窄屏隐藏辅助导航。标题由 Host 在后台生成，手动命名优先；迟到轮询不能覆盖新目录状态，失败不阻塞主 run |
| 侧栏与模型 | 桌面展开/窄栏偏好可恢复，窄屏采用带焦点约束的遮罩。模型菜单与输入区对齐，聚焦不滚动外层容器，搜索和选择保留真实模型名称；不增加未实现的账号、账单等入口 |
| 设置与搜索 | 分类切换保留草稿；重复 Esc 不能丢弃未保存内容；添加或切换模型不得覆盖草稿；整个工作区忙时只读。对话搜索只匹配标题，未实现全文搜索。键盘关闭返回入口焦点 |
| Plan UI | 计划事实仍持久化；步骤摘要仅在当前活动 run 中显示，terminal 后卸载。删除重复页头入口、计划抽屉和历史过程清单，旧 /tasks 打开运行记录。审核使用 revision，重复点击受保护，导航使迟到响应失效；修改入口聚焦意见框 |
| 思考图标 | 根目录 QA 中 PNG 光核、双 Asterisk、琥珀光球均为已替代试验。最终使用 lumen-claude-amber.svg，GSAP 只更新 transform/opacity，卸载清理；静态偏好停止动效。装饰图标 aria-hidden，状态提供可访问名称 |

UI 隐藏或折叠不能清空领域计划、证据、审批或 Session 原文。新 turn 没有计划时，不把上一轮
计划当成当前进度。普通模式的步骤清单与 Plan Mode 的先规划后确认是不同流程。

## 文件预览与边界

9 月 4 日复审将文件入口接入既有链路：成功 write_file/edit_file 的参数或回复中的本地文件链接
→ 认证 API → WorkspaceHost.read_document → 有界文件读取 → 预览/下载。
该清单不覆盖任意 shell、MCP 或插件产生的全部文件副作用；回复链接也不证明文件存在。

当次支持 Markdown 阅读/源码、文本/代码、静态 HTML、常见位图和浏览器 PDF；SVG 按源码显示，
Office 无内嵌渲染。读取总上限 20 MiB，超过 1 MiB 的文本只下载。后续 GLB、文件恢复及快照
能力见[命令说明](../commands.md)与[运行时验收](2026-10-09-runtime-optimization-validation.md)，
不能把早期“无历史快照”的限制当成当前全部能力。

安全边界来自 workspace 路径解析及 descriptor/O_NOFOLLOW 打开：拒绝逃逸、符号链接和特殊
文件，读取在线程执行。相对链接先归一化但不能越过工作区。直接下载使用 attachment、
no-store、nosniff 和 CSP；HTML 在空 sandbox iframe 中静态展示，禁脚本/网络、清除自动导航，
关闭时 abort 并释放 Blob URL，迟到响应不能重开预览。当前文件预览不是历史交付版本的证明。

## 原始验收与限制

| 阶段 | 当次实际验证与边界 |
| --- | --- |
| 9 月 3 日消息、滚动、侧栏 | 临时工作区与模拟 Provider 穿过真实 Web/API/Host；390×844 模拟视口、消息跳转、标题延迟和手动命名优先通过。侧栏阶段 Web 101 tests；未调用外部 Provider |
| 9 月 3 日设置、搜索 | 设置 Web 104 tests，保存冲突、重复 Esc、草稿保护和工作区忙时只读覆盖；浏览器只修改临时配置。搜索使用真实 Host 目录，不宣称正文检索 |
| 9 月 4 日 Plan 初版 | Web 120 tests、typecheck/build；本地原型被浏览器策略拒绝打开，未完成参考像素对照。后来布局复审替代其常驻入口，不保留该原型作为生产规范 |
| 9 月 4 日计划与文件复审 | Python 960 tests、60 snapshots，Host/API/读取相关 51 项；Web 127 tests、typecheck/build。Chrome 验证完成后无胶囊、运行中展开及 Markdown/静态 HTML；fixture 脚本未执行，未核验下载落盘 |
| 思考图标 QA | 真实 Web/Host/SSE 与延迟模拟 Provider，运行态及三帧 GSAP 采样、旧 DOM 清零和控制台检查；后续显示器刷新率观察仍需真实设备，不等于性能验收 |
| 9 月 18 日过程修订 | 最终 Web 193 tests、Conversation 19 项、typecheck/build；桌面浏览器和 390px 模拟视口通过。早期参考页请求头错误未成功获取，后来本地验收不替代外部参考截图对照 |

完整阶段材料仍可在整理前的 [Git 快照](https://github.com/JasonYZzz/lumen/tree/8940f92b3a61b0f0e3d7dc0e770da7d06facc3ea/docs/research)
与[设计 QA](https://github.com/JasonYZzz/lumen/blob/8940f92b3a61b0f0e3d7dc0e770da7d06facc3ea/design-qa.md)
追溯。相关当前契约由 Conversation、Composer、PlanReview、SettingsDialog 等组件测试及
Host/API 测试维护；不依据截图中的旧像素尺寸重新规定产品。

真实 Provider 文本质量、真实手机/GPU、120/144Hz 动效与长期资源趋势没有因本次文档合并获得
新验收结果。Web 与 TUI 保持各自适用布局，所有客户端仍通过 Host 工作。
