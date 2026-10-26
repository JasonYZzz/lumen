# Lumen 功能联动与 UI 升级验收

日期：2026-10-22。范围为真实 Lumen Web 客户端、Host 文件收录与 Store 信息流；不是独立 Muse 演示原型。按用户确认的实施计划，在已有未提交变更上迭代，保留回收站、会话及现有执行与权限权威。现行运行行为见[个人助理实现说明](../architecture-guide/16-personal-assistant-automations.md)。

## 已实现的变化

| 环节 | 实际行为 |
| --- | --- |
| 创建目标 | 标题＋可选说明，先保存，再在固定关联 Session 提交一次规划；重复／丢失响应后的重试复用 request identity。失败保留目标与重试入口，刷新不自动执行，切页后迟到响应不抢回导航。编辑只保存修改。 |
| 规划与推进 | 普通 StartRun 规划、持续目标推进及定时跟进分别启动；原始说明、原会话历史和讨论草稿保留。 |
| 成果与信息流 | 文件收录、目标关联与资源版本事件在 Store 事务内完成。首次收录／真实内容变化产生固定版本通知；重复同步、仅改名、无变化读取与试跑不伪造成果。 |
| 待处理 | 目标待验收、阻塞或轮数上限生成事件；当前 activation／run 已改变或问题已解决时，退出 attention 集合，保留历史。 |
| 继续工作 | 查看本次成果读取固定资源版本，围绕成果讨论携带该资源引用，继续目标恢复关联会话。收藏、回收站、CAS 冲突及旧版本路径保留。 |
| 页面 | 对话上下文带与成果卡、信息流成果／事项卡、灵感依据／来源／预期成果、目标概览与成果优先的详情、资料网格／列表及类型预览共用暖白与克制绿调。 |
| 导航与表单 | 全局活动入口显示真实运行状态；手机五项带文字导航与安全区留位。新建目标焦点边界不再被滚动容器裁切，触控操作区加大，支持减少动态效果。 |

保留 Lumen Logo、Geist、现有九尾狐素材。资料图像使用原图，文本使用已有真实节选，无法提取的类型提供明确封面。没有新增依赖、模型服务、Runtime、Scheduler、个性化简报或跨应用代办。

## 自动验证

- Web 全量：33 个测试文件、297 项测试通过；覆盖一次创建规划、启动失败重试、响应丢失与切页迟到、保留原讨论草稿、信息流／目标成果固定版本入口、手机跟进摘要，以及原有管理／预览／权限路径。
- 最终 Python 全量 `uv run pytest`：1,613 项通过、1 项跳过，60 个 TUI 快照通过；助手流程与管理相关回归 23 项通过，覆盖版本去重、改名不通知、删除／恢复、待处理生命周期及目标保存重试。
- Ruff、严格 Pyright、contracts 检查通过。Web 类型检查与最终生产构建通过。Atlas 按官方脚本重建后新鲜度检查通过；OpenAPI／TypeScript schema 重新生成前后逐字节一致，`api:schema:check` 因已有未提交 diff 返回 1，本轮未改变 schema。
- 本轮没有变更 HTTP schema 或 Session／SQLite schema；沿用既有身份、引用、事务及执行接口。

测试使用已安装 Node 24.19.0，Vitest 加 `--no-experimental-webstorage`，避开本机 Node 26 的实验 Web Storage 与 jsdom 冲突；依赖与锁文件未因本轮更换。

恢复边界补充：目标保存的幂等比较仅比较 GoalInput，由服务端管理的 execution 与对象身份等字段不参与比较。否则保存已经成功、响应丢失后的原请求重试会误报 revision 冲突。新增回归验证重试不改写已保存目标、原关联会话或已有执行状态。

## 浏览器与设计证据

浏览器使用临时隔离工作区与真实 Host、确定性 FunctionModel，经真实工具回执生成 Markdown、HTML、CSV 和 PNG 文件，自动收录至资料空间与信息流。示例标题与模型输出明确标注隔离验收，不含生产用户资料，也未调用外部 Provider。另验证浏览器创建目标后进入自动规划对话，持续推进与定时跟进保持未启用。

截图覆盖 1440 桌面、1920 宽屏、820 平板宽度和 390 手机宽度，核对有数据页面、无持续跟进空状态、目标创建焦点、目标验收、资料封面与真实图像、手机输入区及底部导航。1920 下 DOM 宽度与 scrollWidth 同为 1920；使用完整页面截取，避免工具视口截断右侧。

证据位于 [.impeccable/review/muse-upgrade-2026-10-22](../../.impeccable/review/muse-upgrade-2026-10-22/)：

- [桌面信息流](../../.impeccable/review/muse-upgrade-2026-10-22/final-feed-desktop.png)、[1920 信息流](../../.impeccable/review/muse-upgrade-2026-10-22/final-feed-wide.png)、[全局活动](../../.impeccable/review/muse-upgrade-2026-10-22/final-activity-wide.png)
- [目标概览](../../.impeccable/review/muse-upgrade-2026-10-22/final-goals-desktop.png)、[目标详情](../../.impeccable/review/muse-upgrade-2026-10-22/final-goal-detail-desktop.png)、[灵感](../../.impeccable/review/muse-upgrade-2026-10-22/final-ideas-desktop.png)
- [桌面资料](../../.impeccable/review/muse-upgrade-2026-10-22/final-library-desktop.png)、[平板资料](../../.impeccable/review/muse-upgrade-2026-10-22/final-library-tablet.png)、[手机资料](../../.impeccable/review/muse-upgrade-2026-10-22/final-library-mobile.png)
- [手机信息流](../../.impeccable/review/muse-upgrade-2026-10-22/final-feed-mobile.png)、[创建焦点](../../.impeccable/review/muse-upgrade-2026-10-22/final-create-mobile.png)、[手机目标](../../.impeccable/review/muse-upgrade-2026-10-22/final-goal-detail-mobile.png)、[手机对话](../../.impeccable/review/muse-upgrade-2026-10-22/final-chat-mobile.png)、[真实规划输出](../../.impeccable/review/muse-upgrade-2026-10-22/08-planning-mobile.png)

设计检测只运行一次；原设计文档旧色值产生 advisories，另有既有导出字体／处理中效果／导航动效提示。检测结果不是无缺陷证明。独立复核先给出 fix，三项修正分别为手机摘要收起、目标成果固定版本、浅绿底文字对比度；回验均为 resolved，最终 disposition 为 ship，范围限这三项、相关截图与源码，不扩大为全产品无缺陷证明。记录在同目录 [finish-review.md](../../.impeccable/review/muse-upgrade-2026-10-22/finish-review.md)。最终次级字色在白／画布／侧栏／浅绿底的对比度分别为 5.53／5.06／4.78／4.68。

## 数据保留与验收边界

本机维护前取得执行锁并核对无活动执行。生产库原有 85 条回收站资料、1 个回收站目标和 67 份会话 journal；不为演示恢复旧资料，不写入隔离样例。更新服务前使用 SQLite backup，并核对 journal 哈希、目标与资源状态。

通过原 CLI stop／background 启动更新本机 `http://127.0.0.1:8765/` 服务，健康检查通过。最终备份位于 `.lumen/backups/ui-upgrade-20261022T030002Z/`，含权限受限的完整 `.lumen` 归档、SQLite backup 和 verification.json。更新前后 67 份 journal 哈希、全部 Goal／Resource 记录和登录密钥逐字节不变。浏览器原 cookie 可直接进入，历史列表正常；真实目标／资料正常列表保持空状态，原回收站资料未复活。没有提交、推送或添加生产示例数据。

本轮确定性模型、组件测试和截图不能证明全部真实 Provider、物理手机软键盘／安全区、读屏器、实际浏览器 200% 缩放、跨平台 CI、长时间调度、断电或性能验收。既有后台联网仍要求已配置与授权的内建搜索能力；没有替用户修改服务或权限。
