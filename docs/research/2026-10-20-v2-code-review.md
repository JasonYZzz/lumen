# Lumen v2 代码审查

日期：2026-10-20。对象：本次未提交的 v2 升级，包括 tracked diff 与新增的 automation、执行约束、Web 助理界面和测试。源码版本 `2.0.0`，未发布。
后续状态（2026-10-21）：下述四项反例已修复并加入正式回归。本文保留发现时的代码与复现，当前实现和检查边界见[目标／资料工作流实施验证](2026-10-21-v2-product-flow-validation.md)，不再将历史 open 状态当作当前结论。
按单用户、单机、一个常驻 workspace owner 的实际范围判断；不以分布式服务标准要求当前实现。

**What this change does:** v2 在原 Host → Coordinator → Runtime → Loop／Gateway 路径上加入持久自动化控制面，以及信息流、灵感、目标、资源库和设置内偏好。首发只读取明确的工作区文本快照，沿用动态狐狸和现有配色，不含邮箱／日历连接器或外部发送。

发现时结论：四项可复现问题，建议修复后再发布。2026-10-20 的回归未覆盖这些反例；后续已修复状态见上方更新。

## Must fix

### 1. P1 启用任务会使旧排队实例反过来停用任务

- **现有逻辑：** [enable 与 revision](../../src/lumen/automation/store.py)、同文件 `begin`／`settle`，以及 [Engine.tick](../../src/lumen/automation/engine.py) 共同决定 admission 和停用。
- **问题：** 成功试跑后，前台执行占用工作区；用户点“立即运行”形成 revision 1 的 queued 实例，再点“启用”。enable 把定义升为 revision 2，却不处理旧 queued；前台结束后 `begin` 抛出 `Definition was superseded`，Engine 将其结算为 needs_user_action，`settle` 随即清空当前定义的 validation、next_due 并停用。用户刚启用的任务不会按时执行。
- **最小修复：** enable 只改变控制状态时，在同一事务中将未改变 spec 的 queued 实例同步到新 revision，admission 仍验证指纹；编辑／暂停继续取消旧 queued。陈旧实例的拒绝不得清除较新定义的有效验证。不能只在 UI 禁用按钮，CLI 和 Host 工具仍能触发。
- **不修复的后果：** 正常的“先运行一次，再启用”操作会停掉任务，并强迫用户重新试跑。

实际复现穿过真实 Host／Coordinator／Gateway／journal：FunctionModel 阻塞前台，排队 run，enable，再释放前台。结果为 `needs_user_action`、`enabled=False`、`validation=None`。另用直接 Store 事务序列复核了同一根因。
回归应断言在前台繁忙时排队 manual／trial，再 enable，最终启用状态与验证仍符合用户最后决定；并保留 update／pause 对旧 queued 的取消契约。

### 2. P1 文件未变会隐藏新的日期提醒与模型结果

- **现有逻辑：** [Host._recover_automation](../../src/lumen/application/host.py) 在完整成功结果后调用 [Store.changed](../../src/lumen/automation/store.py)，后者只比较来源 receipt 的文件哈希与 checkpoint。
- **问题：** 固定文件写有“10 月 22 日截止”，委托按当天日期提醒。20 日首次正式执行后推进 checkpoint；21 日文件不变，但模型正确生成“明天截止，请今天处理”。此结果被标为 no_change，Store 不创建资源或信息流，UI 也隐藏该实例的“查看结果”。原文仍在 Session／artifact 中，但不会作为新简报送达。
- **最小修复：** 在能够验证结果等价前保留成功结果的投递，删除仅由来源哈希推导业务 no_change 的分支即可恢复可靠性。若保留去重，必须把日期、有效偏好及实际待投递内容纳入已验证的等价规则，不能让模型一句“无变化”代替确定性校验。
- **不修复的后果：** 用户依赖的日期提醒或新分析被静默隐藏。与此同时模型仍被调用，不能获得“没变化就省调用”的收益。

实际复现使用同一文件、前后两天的受控 clock 和不同 FunctionModel 输出：trial → complete → no_change，正式信息流始终只有 1 条，资源只有 trial 和首份报告共 2 条；新提醒只能在执行会话找到。
回归应覆盖“来源相同、日期／结果不同应送达”，再定义真正相同结果的安静行为；目前现有测试只覆盖相同来源和相同输出。

### 3. P2 编辑表单会覆盖用户的静默设置

- **现有逻辑：** [AutomationEditor.submit](../../src/web/src/components/assistant-workspace.tsx) 构造完整 spec，固定写入 `notify: true`。
- **问题：** CLI／API 可以创建 `notify=false` 的任务，其成功信息流默认已读。打开 Web 的“编辑”并保存，即使没有修改字段，也会将其改为 true；表单没有告知用户这项变化。
- **最小修复：** 编辑时保留 `definition.spec.notify`，新建才使用默认 true。无需为本次修复新增设置层或配置字段。
- **不修复的后果：** 用户原有的静默行为丢失，后续成功报告重新成为未读。

实际通过临时 jsdom 组件测试复现：加载 notify=false 的定义，点击“编辑”→“保存草稿”，捕获 API payload 为 true。该测试是观察性反例，完成后已移除；尚需在修复中加入“保存后仍为 false”的正式回归。

### 4. P2 Host 工具重名会阻断普通对话

- **现有逻辑：** [Host._assistant_tools](../../src/lumen/application/host.py) 对每个根文字 run 注入四个新名称；[Runtime._lumen_tool_schemas](../../src/lumen/runtime.py) 调用 Gateway.derive，而 [ResourceManager.RESERVED_TOOL_NAMES](../../src/lumen/resources.py) 尚未登记这些名称。
- **问题：** 原有 Python 插件若提供 `manage_automation`，配置注册阶段可以通过；现在发送普通“Hello”也会在 schema 派生时抛出 `capability already registered: manage_automation`，未调用模型即 run.failed。并非仅自动化入口不可用。
- **最小修复：** 将四个 Host 工具纳入现有保留名检查，启动时提供明确的插件改名诊断，并说明兼容处理；不要静默覆盖插件或 Host callable。补充普通根 run 与插件冲突的集成回归。
- **不修复的后果：** 有同名自定义工具的工作区在升级后所有普通文字对话失败。

实际用注册该名称的插件 Gateway 驱动真实 WorkspaceHost：终态 `run.failed`，错误如上，模型调用数为 0。源码核对确认这四个名称没有进入既有插件／MCP 保留名校验集合。

## 已核查的保护与边界

- 主执行路径、run 固定 UUID、持久终态后发布、SETTLING 恢复与事务幂等；自动化不依赖普通 `_requests` 的 10000 容量。
- OS owner 与执行锁分离；已有 actor／run 投影回收，未发现此前怀疑的无限 actor 保留问题。
- 来源读取穿过父 Gateway，保留风险、Hook 和 no-follow 文件边界；无父 builtin read_file 或需要确认时拒绝，无 Windows 普通路径降级。
- 后台模型 schema、实际 Gateway 和 Provider 原生工具同时收窄；有限执行预算不修改共享 limits。普通根管理工具为 confirm，现有策略强制新审批；Plan 非 read 拒绝路径保留。
- Session v12 与配置 v2 兼容；MemoryManager 是偏好权威，后台只固定允许的明确偏好；资源历史正文和 Work Product 正文权威保留。
- API 复用鉴权／CSRF，输入校验错误不回显原输入；前端保存请求 ID、分页响应竞争、详情与偏好错误恢复。

这不是“不存在其他缺陷”的保证；边界以本轮读取的源码、下列自动回归和隔离反例为准。

## 本轮实际验证

| 命令／实验 | 本轮结果 |
| --- | --- |
| `uv run ruff check .` | 通过 |
| `uv run pyright` | 0 errors；本机 macOS，不代表三平台矩阵 |
| `uv run --extra web --extra live pytest tests/test_automation.py tests/test_workspace_host.py tests/test_web_api.py tests/test_cli.py tests/test_runtime.py` | 174 passed |
| `pnpm --dir src/web test` | 31 files，238 passed |
| 隔离 Python 反例 | 来源相同而新提醒被隐藏、排队后 enable 停用、插件工具重名均复现；排队问题另通过真实 Host 复现 |
| 临时 jsdom 反例 | 1 passed，确认 notify 被覆盖；验证后删除临时文件 |
| 文档链接 | 检查 19 份文档，216 个本地链接目标全部存在 |
| 生成物与 diff | contracts／Provider catalog／Atlas 新鲜度通过；OpenAPI 与 TypeScript 重新生成后 SHA-256 不变；`git diff --check` 通过 |

隔离实验使用 TemporaryDirectory 和 FunctionModel，未调用真实 Provider、未修改用户助理状态、未重启本地 8765。反例的“通过”表示观察到了缺陷，不表示缺陷已修复。
前轮全量 1558 passed／1 skipped、打包和浏览器验收保留在[原验收记录](2026-10-20-v2-implementation-validation.md)，不冒充本轮重新执行。

## 已知运行问题与未检查项

前次重启本地 8765 已暴露旧 CLI 的 5 秒 readiness 限制：超时分支删除进程状态文件，却未结束仍在启动的子进程，可能形成状态查询不可见的服务。这个分支早于 v2；本次不计为新增缺陷，也未再次重启生产测试服务。后续应区分“子进程退出”和“仍在启动”，并保证退出时的状态与进程一致；v2 启动还会索引历史 Session，不能假定总在 5 秒内完成。当前可用前台 `lumen web` 避开后台分支，不能通过放宽成功判断伪装 ready。

未重新执行完整 Python 套件、Web 生产构建、wheel／DMG 打包、跨平台 CI、真实 Provider／Keychain／媒体、长时间调度、断电／磁盘满／时钟变更实验。当前是代码与文档审查，未证明这些发行条件。

**Verdict: fix 1–4 first.** 先加入反例回归并修复，再做真实 Provider 与发行验收；架构说明 Accepted 和原视觉修复的 ship verdict 均不替代本次结论。
