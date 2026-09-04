# 失败任务删除与恢复门禁审计

审计日期：2026-09-04。本文记录本次缺陷、修复与验证；当前行为以源码和契约测试为准。

## 根因

失败的调研任务留下 6 条 Exa `reconciliation_required` effect。它们的起因及 MCP effect
声明修复见 [前次审计](2026-09-03-completion-recovery-codex-pi-audit.md)。本次删除失败来自另一个
错误：`WorkspaceHost._require_session_management_safe` 把 Agent 与 TaskWorkspace 的完成门禁
直接用于归档和删除。只有任务达到可完成状态，用户才能隐藏失败任务，形成了恢复死结。

Session 删除本来就是目录 tombstone，不是物理删除或完成声明。因此隐藏任务不应要求用户
豁免未知外部结果，也不应把 effect ID 写入报告来满足内部状态检查。

## Code review 与修复

| 问题 | 当前实现与回归证据 |
| --- | --- |
| 历史未完成结果阻止隐藏任务 | Host 可见性检查只检查活动执行；6 条未知 effect 保持原状态，归档、恢复与删除通过；恢复后续跑仍被拒绝 |
| 目录修改与另一 Host 执行竞争 | 使用已有 WorkspaceRunLock，在锁内读取最新 journal、检查状态并追加目录；不创建新状态权威 |
| 锁重入可能释放运行持有的 lease | 在 acquire 前检查本实例已持有的锁与 active execution；回归确认失败请求不释放原锁 |
| Agent completion gate 只绑定当前 root run | 可见性检查遍历 Session 中所有 Agent 的持久状态；旧 run 的 running / approval_pending 仍阻止删除，import_pending 不阻止 |
| 尚未结束的语音连接被隐藏 | 检查持久化与当前 LiveManager 投影，活动连接需要先结束；Web API 契约覆盖连接前后删除结果 |
| 请求重试产生 404 或重复目录记录 | 同一已删除 Session 的重复 DELETE 返回 deleted，不追加记录；不存在的 ID 仍为 404 |
| 其他 Host 的 actor 缓存忽略删除 | actor 访问前读取 journal 中的墓碑，删除后快照返回 404 |
| 晚到的标题目录快照撤销删除 | SessionRepository 验证全部记录，但首次 tombstone 后不再替换目录投影；冷热缓存一致，原始 journal 保留 |

检查覆盖 Host command dispatch、API Adapter、跨 Host 缓存、OS execution lease、Agent/Live
活动状态、append-only journal 与 TaskWorkspace 恢复门禁。未增加跳过审批、自动豁免或未知动作
重放路径。共享 Host 行为适用于使用该 Interface 的客户端；本次没有改变前端 schema。

## 清理与兼容

删除了可见性检查中调用两个 completion gate 的旧分支，其职责已经由 Host 的活动执行检查
完整替代；TaskWorkspace 和 AgentOrchestrator 自己的完成门禁继续保留。同步修正第 5、12 章
及 commands 中过期的行为描述。本次未删除仍覆盖生产契约的测试，没有批量清理用户工作区。

Session schema 保持 v9，既有低版本加载及只追加升级路径不变。删除保留 turn、work state、
effect、artifact 与实际产物，不回滚文件或远端动作；归档恢复也不清除待核实状态。

## 验证

- 全量 `uv run pytest`：958 passed，60 snapshots passed。
- Ruff、Pyright、contract catalog 检查通过；OpenAPI 与当前 FastAPI schema 一致。
- 指定失败任务已通过生产 ResourceManager / WorkspaceHost 的公开 DeleteSession command
  软删除，返回 deleted。操作未启动模型或 MCP，也未绕过 HTTP 认证修改服务。
- 实际 journal 原字节前缀、全部 turn 与 work state 保持不变，6 条待核实记录仍存在；列表
  不再包含该任务，重复删除没有追加记录。

本地端口上的既有 Python 进程需要重启才会使用新代码；本次未终止其他进程。自动化回归不能
保证所有真实 Provider、平台与跨进程调度组合无缺陷，实时语音 Provider 覆盖债务仍见第 12 章。
