# Lumen v2 源码升级与验收记录

日期：2026-10-20。环境：本机 macOS / Apple Silicon；开发 Python 3.12、隔离 wheel Python 3.13。
状态：源码版本 2.0.0，未提交、推送或发布。本文只记录实际执行，不代替多平台 CI、真实 Provider 与设备发行验收。

后续同日[代码审查](2026-10-20-v2-code-review.md)复现了四项待修问题。下列测试和视觉验收是原始记录，不能据此认定这些新发现已修复；当前发布结论以该审查为准。

## 已实现

- 沿用 WorkspaceHost → RunCoordinator → AgentRuntime → Loop / Gateway；追加严格的助理 command / API，不改变 Session v12 或配置 v2。
- 单工作区 OS owner、SQLite WAL / FULL、本地日期 edition 去重、结构化时区规则、漏跑合并、CAS 与持久 request identity。
- 草稿、试跑、验证指纹、启用／暂停／立即运行；固定 Session / interaction identity；原 journal 终态恢复、SETTLING 幂等结算，不重放中断模型执行。
- 只读文件快照、范围和体积限制；父能力缺失或确认需求立即待处理；模型无本地／原生工具，有限请求预算与执行时限，后台不启动标题／压缩／记忆学习模型任务。
- 信息流、规则灵感、用户确认目标、资源库服务端分页与搜索／收藏／版本编辑；原 Work Product 使用引用，原执行结果保留历史正文。
- 设置内偏好确认／编辑／遗忘；现有动态九尾狐、对话与中性配色保留。根交互通过审批的 Host 工具可管理自动化／目标。
- CLI `lumen automation` 与版本元数据；桌面 DMG 文件名从 pyproject 版本派生。

## 自动验证

| 实际命令 | 结果／范围 |
| --- | --- |
| `uv run ruff check .` | 通过 |
| `uv run pyright` | 0 errors；本机平台检查，不是三平台类型矩阵 |
| `uv run --extra web --extra live pytest` | 最终全量 1558 passed、1 skipped，60 snapshots passed |
| `uv run pytest tests/test_automation.py` | 最终 20 passed；包含关闭记忆／隐私模式、缺表和未知无版本数据库保护 |
| 自动化 / Host / Web API 前期定向回归 | 83 passed；后续增补由上述 20 项与最终全量覆盖 |
| Host / Web API / CLI 前期定向回归 | 98 passed；后续改动另有上述定向与最终全量覆盖 |
| `pnpm --dir src/web test` | 最终 31 files、238 tests passed；含详情错误、偏好连续失败／恢复及响应丢失后请求 ID 复用 |
| `pnpm --dir src/web typecheck`、`build` | 最终通过，静态导出成功 |
| `cargo check --manifest-path src/desktop/Cargo.toml --locked --offline` | 通过；不等于 app／DMG 或公证验收 |
| `uv build` | wheel 与 sdist 可生成；先构建 Web |
| 隔离 venv 安装 wheel，`lumen --version`、`--check-config` | 版本 2.0.0、Configuration OK；使用无凭据 test 模型，不调用真实模型 |
| contracts / OpenAPI / Atlas | 由仓库命令生成并核对确定性；OpenAPI 与未提交基线有预期新增契约差异 |

来源集成、取消恢复和产物测试用 FunctionModel / fake Driver，不接触真实 Provider 凭据。
`test_automation.py` 覆盖 DST gap／fold、路径边界、request 幂等／CAS、暂停、试跑不消费正式 checkpoint、正式结果与 no_change、版本与历史正文、双 Host owner、admission intent 恢复不重放、结算失败不重复模型调用、缺失／逃逸来源、损坏存储保留普通对话、鉴权／严格验证、父能力缺失、偏好身份／遗忘、Work Product 引用，以及唤醒与关闭竞争、跨事件循环重开。
最后增加了确认型／自定义来源不能被替换为免审读取、禁用记忆／隐私模式不会进入后台模型输入，以及缺失 checkpoint 表或未知无版本数据库不会被自动重建的反例。
Runtime 测试检查后台模型请求无工具／原生工具、伪造工具调用被拒绝、退出后普通交互能力恢复。

## 浏览器与设计验收

真实静态生产构建由隔离 FastAPI Host 提供，工作区 `/tmp/lumen-v2-acceptance-workspace`，使用测试模型。
页面中任务／目标／笔记均标注“验收示例”，不含用户私有数据，不证明真实模型内容质量。
检查默认 1280×720 与手机 390×844：保留原狐狸，信息流、来源详情、自动化表单、表单底部保存及手机导航关闭焦点返回。
实际操作试跑、启用、暂停；执行详情可打开原始结果。

独立 Impeccable 审查确认视觉符合批准方向，提出两项错误状态问题：详情内错误被弹窗遮挡、偏好重试后状态不完整。
已修复并新增组件测试；隔离验收服务注入 503，实际验证弹窗内显示错误、同一动作重试成功，以及偏好连续失败后成功清错。
独立 verdict 将这两项修复评为 resolved / ship；这是修复清单的评分，不是再次全表面审计或真实 Provider 验收。
设计 detector 对四个修改目标只执行一次，结果 `[]`。截图及独立审查／verdict 位于 `.impeccable/review/lumen-v2/`。
这些截图属于本机验收，不作为用户数据或生产内容打包进应用。

## 仍需独立完成的门禁

- 真实 Provider smoke、账单／长时间调度与压力测试、真实断电／磁盘满／时钟变更实验。
- Linux／Windows CI 全矩阵、macOS app／DMG 构建与最低系统／媒体／Keychain 实机验收。
- Windows 的安全来源读取仍拒绝运行，无普通路径检查的降级替代。
- 邮箱／日历 OAuth、账号 scope／refresh／revoke、外部发送、持久审批／continuation、远程 Host、系统通知均不在当前实现。

当前 v2 可交付范围与回退路径以 [Accepted 说明](../architecture-guide/16-personal-assistant-automations.md) 为准；原 [实施规划](../../design/lumen-v2/implementation-plan.md) 中的后续连接器和发布退出条件继续保留。
