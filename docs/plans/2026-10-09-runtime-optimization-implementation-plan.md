# Lumen 运行时可靠性与性能优化实施计划

> 文档复核（2026-10-10）：下文保留计划目标与原始基线；最新完成范围和剩余门禁见[活动计划索引](README.md)，运行行为以源码和当前架构为准。

状态：核心切片已实施；真实 Provider／跨平台推广门禁待验收。日期：2026-10-09。

状态复核：2026-10-10。源码已随 `7f5e5ce` 提交；Web 文件恢复入口随 `fa0aba3` 提交。
本地测试和性能样本仍为原验收日期的证据，未因提交或本次文档更新补齐真实 Provider／跨平台门禁。

源码基线：`291348aa956c88e6ccd6f465fd4ea63656956e7c`。本计划承接当日代码审查的七项建议，
规定实施顺序、改动边界和验收条件；执行结果与偏差见第 16 节，当前权威决策见
[14.7 完整批次与恢复](../architecture-guide/14-long-running-recovery.md#147-完整批次耐久与文件恢复决策)。

本轮先解决会话 I/O、事件内存和诊断归因，再增强完整批次恢复、任务评测、搜索和文件撤销。
所有阶段扩展已有 Module，保持 Lumen 自有 Loop、根 Host 命令入口及 append-only Session 权威。

## 1. 范围与完成目标

| 工作流 | 用户得到的变化 | 优先级 | 对应阶段 |
|---|---|---|---|
| 长会话读取与大输出转存 | 会话变长后仍能响应取消、审批和界面请求，重复读取不再扫描全部历史 | P1 | M2 |
| 长时间运行 Web 或 TUI | 已结束任务及时释放内存，流事件回放有明确容量和过期语义 | P1 | M3 |
| 进程意外退出 | 保留最后一个完整模型／工具批次，重启后明确中断并安全显式重试 | P1 | M4 |
| 任务质量评测 | 用实际结果比较版本、推理档位和并发，成本及成功率可复查 | P1 | M0、M5 |
| 大仓库探索 | 目录深度限制真正约束遍历，文本检索逐行读取并提前停止 | P2 | M6 |
| 错误文件修改 | 预览并撤销有快照的文件变更，检测用户后续修改及不支持的动作 | P2 | M7 |
| 性能排障 | 分清模型、工具、审批等耗时，避免并行工具被重复扣减 | P2，作为测量前置 | M1 |

完整交付需要 M0–M7 的验收均通过。M2、M3、M6 可先独立交付；M4 和 M7 不以兼容或恢复问题
尚未解决为代价赶进同一版本。不得把代码落地或离线测试通过标为真实 Provider 性能验收完成。

### 1.1 沿用的权威与 Interface

- `WorkspaceHost` 持有 Session actor、执行准入、根审批、幂等命令和公开事件。
- `RunCoordinator` 持有 turn 的持久化顺序和历史投影提交；客户端不能自行补写 journal。
- `AgentRuntime` 提供完整模型／工具批次及 Context prepare/commit 数据，不持有第二套存储。
- `LumenAgentLoop` 持有模型重试、工具批次和取消；不新增独立调度器。
- `SessionRepository` 是持久化事实的唯一入口；索引、分页和缓存均可删除后重建。
- `ArtifactStore` 持有内容寻址载荷；FTS 是派生索引，不决定 Session 或 Effect 是否成功。
- `TaskWorkspace` 持有 mutation、快照、验证和恢复；撤销文件仍必须跨越该 Interface。

### 1.2 本轮不纳入的建设

不引入 PostgreSQL、Redis、向量数据库、通用 Eval 平台、新的 workflow runtime 或 telemetry
daemon；不按文件行数拆分 Host/Runtime/Context；不扩大 child 深度和权限；不改用 Codex harness
替代 Lumen Loop。不新增后台命令产品能力或 LSP 集成，除非 M5 的失败样本证明它们是主要瓶颈。

现有 MCP/Skill 延迟加载、FTS 检索、隔离 worktree 和兼容 Adapter 继续保留。未声明副作用仍是
unknown，Git commit/push 仍经根 Host 每次新审批，默认沙箱仍 fail closed。

## 2. 审查基线与证据边界

| 已确认的事实 | 源码或测试入口 | 对实施的含义 |
|---|---|---|
| `load` 在比较缓存前读取全部 JSONL 并计算 SHA256；`load_turn_page` 先加载完整 Session | [sessions.py](../../src/lumen/sessions.py) | 优化读取和投影，不另建 canonical history |
| Artifact 写入同步执行 FTS 索引；Context 异步 prepare 路径调用同步输出缩减 | [artifacts.py](../../src/lumen/context/artifacts.py)、[engine.py](../../src/lumen/context/engine.py) | 拆清主载荷写入和可降级索引，处理线程取消及顺序 |
| EventJournal 留存全部事件，Host 留存全部 run 与请求映射 | [events.py](../../src/lumen/application/events.py)、[host.py](../../src/lumen/application/host.py) | 增加有界生命周期，同时保护 SSE、审批和幂等 |
| 正常完成和异常／取消会保存完整批次；硬退出前的内存批次没有同等保证 | [runtime.py](../../src/lumen/runtime.py)、[run_coordinator.py](../../src/lumen/run_coordinator.py) | 恢复只到完整批次边界，不保存半截参数或 Loop program counter |
| 追加记录执行 fsync；半截 JSONL 行会导致读取失败 | [sessions.py](../../src/lumen/sessions.py)、[test_sessions.py](../../tests/test_sessions.py) | 保留严格读路径，修复使用显式、保留原件的操作 |
| benchmark 已经过真实 Host，但任务限于只读夹具，评分主要是答案包含字符串 | [benchmark_agent_loop.py](../../scripts/benchmark_agent_loop.py) | 扩展已有脚本与任务契约，不新建框架 |
| 目录深度只过滤最终结果；搜索收集全部路径并整文件读取 | [builtin.py](../../src/lumen/tools/builtin.py) | 优先标准库流式遍历，保留路径与输出契约 |
| 精确修订恢复、冲突检查和 Effect 状态已经实现 | [workspace.py](../../src/lumen/work_products/workspace.py) | 增加 turn 归属与 Host 入口，复用恢复实现 |
| 诊断以 run 总耗时减工具耗时之和推算模型耗时 | [run_diagnostics.py](../../src/lumen/run_diagnostics.py) | 改为请求级事实；不能将重叠耗时视为互斥阶段 |

当日离线验证：Ruff、Pyright 通过；Python 1482 passed、1 skipped；Web 217 passed，类型检查和
生产构建通过；contracts、Provider catalog、Architecture Atlas 新鲜度检查通过。
Web 本机环境为 Node 24 / pnpm 12，不等同 CI 的 Node 22 / pnpm 10。

诊断性微测量：在临时目录同步保存并索引约 10.7 MB 的合成 Artifact，耗时 0.261 秒，事件循环
延迟约 0.251 秒；两次 Session `load` 产生两次文件读取。前者是单次本机测量，不能直接外推
实际任务 p95、其他磁盘或 Provider 性能；M0 必须形成可重复基线。

## 3. 实施顺序与依赖

| 阶段 | 交付切片 | 前置条件 | 合并后可独立使用 |
|---|---|---|---|
| M0 | 可重复性能基线、任务夹具和验证环境记录 | 无 | 是 |
| M1 | 正确的诊断口径 | M0 | 是 |
| M2 | 会话与 Artifact I/O 优化 | M0、M1 | 是 |
| M3 | run、缓存及事件回放容量管理 | M0；分页基于 M2 | 是 |
| M4a | 完整批次持久化契约及 schema | M2；设计门禁通过 | 是 |
| M4b | 硬退出恢复与显式尾行修复 | M4a | 是 |
| M5 | 任务结果评分与 Provider 对照评测 | M0、M1；覆盖 M2–M4 成果 | 是 |
| M6 | 流式目录和文本搜索 | M0；结果进入 M5 | 是 |
| M7a | 文件变更 turn 归属及恢复预览 | M4 的 schema 分配协调完成 | 是 |
| M7b | Host 恢复命令、TUI 和 Web 交互 | M7a | 是 |

实施可分为三批发布：A 为 M0–M3 与 M6；B 为 M4 和 M5；C 为 M7。每个切片单独审查、验证和
记录结果，不要求所有功能一起上线。M5 的离线任务先在 M0 建立，正式结果汇总放在 M5。

与[外部 Agent 接入计划](2026-09-15-external-agent-integration-plan.md)共同修改 schema 时，
本轮已引入 v12 完整批次／维修／Effect 归属；外部接入仍为 Proposed，必须使用其合并时的下一版本，
不得再独立占用 v12。
沿用[单一 Loop 决策](../architecture-guide/13-native-agent-loop-migration.md)的原则：
不保存完整 Loop state、不做透明崩溃接管；本轮新增的是完整批次恢复证据。

## 4. M0 建立可重复基线

### 实施

扩展 `scripts/benchmark_agent_loop.py`，保留 dry-run 默认值和显式 `--execute`。
在现有 `tests/fixtures/` 下增加确定性夹具；大文件和大目录由脚本生成，避免提交巨型 fixture。
不读取开发者 HOME 中的角色、真实 Session、凭据或项目配置作为实验输入。

建立三组基线：

1. Session：20/200/2000 turns、冷读取、热读取、追加后读取、列表和单页读取；分别记录文件
   字节数、实际读取字节数、反序列化次数、耗时及峰值内存。timeline、Effect 和 rewind 都要有样本。
2. Artifact 与事件：1/10/64 MiB 的合成内容，FTS 可用／不可用；长流和大量已完成 run；记录
   blob 写入、索引耗时、事件循环延迟、retained event 字节数和缓存条目数。
3. 搜索：多层目录、大量 ignored entries、早期命中、无命中、超长行和无效 UTF-8；记录实际
   访问目录数、读取字节数、首个结果时间及总时间。

每组先预热，再至少测量 30 次；输出原始样本、median、p95，以及 commit、OS、Python、SQLite、
Node/pnpm 和依赖锁摘要。性能比较使用同一机器、相同数据与相同测量方法；不把 paid Provider
采样混入确定性微基准，也不把机器相关毫秒数设为普通 CI 的脆弱硬断言。

### 验收

- 离线运行不调用模型或外部服务；同一夹具两次得到相同语义结果。
- 报告不含输入正文、工具完整输出、凭据或个人数据；未知 usage/cost 为未知，不填零。
- 计数检查可稳定发现全文件重复读取、越过深度的遍历、整文件搜索和未回收事件。
- 保存基线结果与复现命令；后续阶段必须能用同一命令对照。

## 5. M1 修正诊断归因

### 改动位置与实现

优先修改 `run_diagnostics.py` 和 `scripts/benchmark_agent_loop.py`，复用 Runtime 已保存的
`request_observed`、`model_retry` 及工具 diagnostic timing。
保持内容脱敏和有界投影，不新建诊断存储或导出完整 transcript。

- 分开表示 run wall time、模型实际尝试耗时之和、工具执行耗时之和、重试退避。
- `request_count` 为逻辑请求数，attempt/retry 为实际尝试；不以列表长度混算。
- 工具耗时之和不是 wall time；多工具共享的 approval 等待不能相加为 run 审批总时间。
- 缺少区间或唯一批次身份时，审批／工具 wall time 返回未知；需要新增计时事实时，在原 Module
  记录相对起止时间，使用区间并集聚合，不引入新的时钟权威。
- 不再使用 `run - sum(tool)` 推断模型时间。旧 diagnostic schema 用明确版本区分，旧字段仅在
  可正确计算时投影；更新真实客户端和兼容测试，说明字段迁移条件。
- compaction、Host I/O 没有独立计时时，不伪造剩余部分的归属。cache usage 缺失与明确为零分开。

### 验收与文档

`tests/test_run_diagnostics.py` 和 `tests/test_loop_benchmark.py` 覆盖串行、并行重叠、共享审批、
重试、重放工具、历史缺少计时、零 usage 和敏感信息。
例如两个并行工具各耗时 6 秒、run 耗时 10 秒时，报告可以显示工具执行总量 12 秒，但不能报告
模型耗时为 0 或将工具 wall time 标为 12 秒。

更新受影响的 Context/运行详情展示与文档；只更新实际改变的 Host/API schema。

## 6. M2 优化会话与 Artifact I/O

### M2.1 Session 读取与投影

保留 `SessionRepository` Interface，将读取、解析和缓存策略集中在该 Module。
先做热读取与列表优化，再根据 M0 结果决定是否需要持久化分页索引。

1. Repository 自己追加成功后更新或失效对应投影；缓存必须绑定文件身份与已读取边界。
2. 快速失效判断至少考虑 device/inode、size、mtime_ns、ctime_ns；通过已打开 descriptor 前后
   `fstat` 检测读期间变化。缩小、替换、非追加变化回到完整严格读取。
3. 文件元数据只是缓存线索，不是内容完整性证明。Host 获得 workspace 写锁后的执行准入必须
   重新验证 journal；对无法可靠判断外部变更的文件系统停用快速缓存，不能因提速绕过事实校验。
4. 真正追加只读取 suffix 并经过同一记录校验、schema upgrade、rewind、running/terminal 覆盖和
   checkpoint 验证逻辑。不要复制第二套 projector；遇到不可增量解释的记录回到完整投影。
5. `list` 使用轻量 metadata/catalog 投影，不为每个 Session 构建 ModelMessage 与完整 history。
   `load_turn_page` 返回所需页，不通过先完整物化再切片伪装分页；active lineage 仍由同一规则定义。
6. 若冷列表／分页仍超出预算，可在已有 Session 存储旁增加可重建的 offset/catalog 索引。
   不复用正文 FTS 表承担 Session 调度；不提前引入新的 Storage Interface 或数据库服务。
7. 缓存返回值不能被调用者修改后污染其他读取。针对当前浅复制的 mutable 成员加入隔离检查，
   选择足够的防御复制或内部不可变投影，不为此迁移整套消息类型。

### M2.2 异步边界与顺序

盘点 Host、Coordinator、Context 和 EffectRecorder 的实际异步调用链，只在阻塞入口卸载工作。
优先使用现有执行 Seam 和 `asyncio.to_thread`，不在所有方法上机械包线程，也不把依赖事件循环的
对象或整体 Host 状态移入线程。

- journal 写入仍顺序执行；线程任务取消不等于文件写入停止。写入启动后，在释放 workspace
  写锁、推进 cursor 或关闭 Repository 前，等待实际操作结束并处理其结果。
- Effect prepared 必须持久化后才 dispatch；terminal 必须 fsync 后才发布。后台写入不能改变这两个
  屏障，也不能让第二个 run 在前一个写入未结束时进入。
- 线程使用 `ContextVar` 的绑定、SQLite connection/RLock、多个 Session 的并发读写均需验证。
- 主 blob 写入与 FTS 索引分开计时；索引可在线程中或受控后台任务执行。索引积压有上限，失败
  不影响已写 blob，搜索返回可理解的缺失／降级结果；不启动无界任务。
- Artifact 发布采用完整临时文件与原子发布；对恢复所依赖的 blob 明确文件及平台所需目录的
  持久化顺序，防止 journal 引用了尚未耐久的快照。Windows 不通过弱化 descriptor 边界补兼容。
- 不在热读取中反复 upsert 全部 checkpoint episodes；只为新增且已验证的 episode 更新索引。
  选择候选时继续过滤 active lineage，不能让旧索引复活弃用分支。

### 验收

- 热读取无实际变更时不重读全部 JSONL；追加场景读取量随新增记录增长。
- 外部替换、同大小修改、mtime 保持、文件缩短、失效缓存和损坏记录均得到正确投影或明确拒绝。
- 分页、列表、rewind、fork 与完整严格读取在所有 fixture 上语义一致；删索引可重建。
- Session v1–v11 均可加载且字节不改写；M2 不因缓存或 I/O 优化升级 schema。
- 索引失败、SQLite busy、取消、写盘失败及关闭期间没有悬空任务，未发布成功终态。
- 同机 2000-turn 热列表／分页 median 和 p95 目标为基线的 50% 以下；10 MiB Artifact 场景的
  事件循环延迟 p95 目标为基线的 20% 以下。达不到目标时记录主瓶颈，不能放宽正确性门禁。

主要测试：Session、Coordinator、Context engine/artifacts/artifact index、Effect recovery、
WorkspaceHost 与 Web API；随后执行全量 pytest。

## 7. M3 控制事件与执行对象生命周期

### 实施

在 `WorkspaceHost` 和 `EventJournal` 内完成容量管理，结合 M2 为 Repository 缓存增加容量。
不新增跨运行调度器或通用缓存框架。容量先作为内部常量，M0 验证后固定；有真实使用需求才暴露配置。

1. 区分 active run、已结束且可回放 run、仅留幂等摘要的 run。活跃任务、审批 future、待交付
   结果、Plan review 引用及订阅中的记录不能直接回收。
2. subscriber 注册与释放放在 `try/finally`；断线不取消 run。借助既有 Host 生命周期和请求入口
   进行回收，确保 close 排空任务，避免新增轮询 daemon。
3. 已结束 run 无订阅者且超过保留窗口后释放 task、完整 journal 和审批对象。
   `_requests` 的回收必须与 run 摘要同步；同一 `client_request_id` 不能因完整对象被回收而重新执行。
4. 定义公开幂等语义：首期维持同一 Host 生命周期内重复请求不重新执行；跨进程幂等另以 journal
   中已有 `interaction_id` 和新增受测事实决定，不把本轮内存回收描述为已有跨进程承诺。
   保留窗口内请求返回原结果；过期身份返回明确过期错误，禁止当成新请求。
   轻量请求身份不能随重对象直接删除，否则无法识别迟到重试。首期为身份集合设置经过 M0
   测量的容量，达到上限时仍处理已有身份，明确拒绝新任务，而不是悄悄淘汰去重证据。
   若该上限影响真实使用，再将 `client_request_id` 关联追加到既有 journal，并用可重建索引
   查询冷身份；这项持久化语义需单独 schema 门禁，不能用未持久化缓存扩大幂等承诺。
5. active run 回放缓冲按实际内容字节数限制，而不只按事件数量限制。sequence 单调递增且与
   缓冲数组下标分离，drop/合并事件不能复用 sequence。
6. 对慢订阅者或过旧 cursor 明确返回需要重取 snapshot 的响应；先冻结该 cursor 的可回放边界，
   不静默跳过事件。snapshot 与下一事件 cursor 必须一致，客户端按边界重新订阅并去重。
7. snapshot 是已有 display facts 的派生投影，不能拥有 Runtime 状态。覆盖 text retraction、
   thinking、工具和审批。中文／emoji 仍按 Python 字符计数，不能改变 Web 撤回偏移。
   有界回放不能被无界 snapshot 抵消：大正文和完成工具详情使用 Artifact／历史分页引用，
   明确当前尾部与完整正文的读取入口；不能截断后仍声称 snapshot 包含全部内容。
8. 已完成历史按 Session 页读取；Host 重启后旧 run ID 的事件入口明确过期，客户端恢复 Session
   transcript。不暗示重启后所有原始 token delta 都可回放。

### 验收

- 1000 个已完成 run 后，重对象、事件字节和 Session 投影缓存不随历史 run 数线性增长。
  RSS 用重复压力样本比较；可达对象数及 retained bytes 用确定性计数断言。
- 一个超长 active run、一个慢订阅者和多个重连客户端不能造成无界回放缓冲。
- 保留窗口内 SSE 连续、无丢失无重复；过旧 cursor 经 snapshot 恢复后与最新投影一致。
- 审批、取消、Plan review、幂等请求、会话删除与 Host close 不引用已回收执行对象。
- 更新 TUI/Web/headless 共用契约及 API schema；慢消费者行为需做客户端测试。

## 8. M4 完整批次持久化与硬退出恢复

### M4a 设计门禁与持久化记录

先补一条子进程测试复现“完整工具已完成、下一模型请求前进程退出”的缺口，再冻结记录契约。
当前重复写 `status=running` 的 turn 会增加多个逻辑 turns，terminal 只替换最后一个；因此不得
直接重复 `append_turn_started` 或追加伪 terminal 充当 checkpoint。

拟增加严格的 `turn_checkpoint` 记录，使用合并时的下一 Session schema；若无需新语义的替代
方案能通过以下门禁，可在实现前替换，但必须记录取舍：

- checkpoint 从属于一个 `interaction_id`；sequence 单调，携带完整批次增量、累计消息边界和
  摘要，重复身份只能引用同一内容，不能将失败校验降级为成功。
- 恢复引用包括相应 approvals、usage、diagnostics、request/recovery receipts 与必要 Plan
  状态；大载荷按 ArtifactStore 契约转存，所有引用先耐久后追加。
- Runtime 复用已有 `continue_after_tools` 等完整批次构造点，提供内部有类型的提交回调；
  Coordinator 校验并持久化。Loop 不访问 Repository，也不增加新的公共 Hook 系统。
- 记录模型响应与所有工具结果成对的 canonical messages；部分并行工具结果仅是 Effect 证据，
  不能作为完整可续跑历史。未知外部动作、Provider 原生工具活动及未交付结果仍经过对账门禁。
- checkpoint 不包含 token delta、半截 JSON 参数、瞬时 Skill/Memory/MCP 投影或 Loop program counter。
- 活跃 turn 的 checkpoint 只形成一份恢复投影；terminal supersede 同一 turn 时核对 prefix
  digest 与累计边界，不能重复追加消息、usage 或证据。
- 正常运行不因恢复 checkpoint 改变 Context 的 prepare/commit 生命周期。首期恢复记录不发布
  新 compaction 父节点；仍按 durable raw 前缀和最后已提交 summary 重建，验证绝对 `source_end`。
- 重启显示 interrupted；执行仍由用户明确 retry。精确成功 receipt 仅按现有安全条件复用，
  changed args 不能误匹配；结果不确定则要求 reconciliation，不透明重跑。

记录版本、terminal 去重、损坏 checkpoint 行为和 compaction 边界需先写 Proposed ADR，再随实现
与测试通过标记 Accepted。v1–v11 的 fixture 必须继续加载且不改写；升级只追加 schema_upgrade。
旧二进制不能读取新 schema，不能承诺新 journal 可通过降级程序继续执行。

### M4b 显式尾行修复

普通 `load` 保持严格拒绝损坏，不静默截断。通过根 Host 的结构化维护命令提供诊断和修复：

1. 读取原文件身份、完整 digest、合法前缀字节边界与最后损坏行；中间损坏、未知 schema、非法
   record、原件发生变化均拒绝自动修复。只处理末尾的不完整追加记录，不猜测业务内容。
   该诊断按校验后的 Session ID 访问 Repository 原件，不先创建必须成功 `load` 的 actor，
   避免损坏会话恰好无法进入维修入口。
2. 展示将舍弃的尾部长度、受影响 turn 和恢复限制；用户显式选择后才执行。
3. 获取 workspace 独占写锁，确认无 active run、Live execution 和未结束写入，并重验原件 digest。
4. 保留完整原件及校验信息，安全写入合法前缀并原子替换；不覆盖备份，失败后原件仍可取回。
5. 重新严格加载，再追加修复审计事实。恢复只到最后完整 checkpoint；丢失的最后追加内容不自动
   推断为未执行，Effect 状态仍可能 unknown。

这是用户请求的维护例外，不是加载迁移；合法 journal 的普通 append-only 行为不变。
维修操作不能被 auto 审批、模型工具或 child 绕过。

### 故障矩阵与验收

| 强制终止位置 | 重启后的要求 |
|---|---|
| run-start 持久化后，模型请求前 | 输入可见，状态 interrupted，无虚假成功 |
| 模型输出／参数尚未完整 | 半截响应仅可作展示或审计，不执行工具 |
| Effect prepared 后，dispatch 前后无法区分 | reconciliation，副作用不能自动再次执行 |
| 并行批次部分工具成功 | 保留已落盘 Effect；不拼造完整批次，不自动补跑未知动作 |
| 完整批次 checkpoint fsync 后 | 恢复消息配对有效，显式 retry 不重做已确认成功副作用 |
| checkpoint 写入未完成／磁盘满 | 不推进恢复 cursor；尾行严格失败并可显式诊断 |
| 同轮多次压缩后 | 无重复／丢失消息，绝对 source_end 与 durable parent 正确 |
| terminal fsync 后，客户端收到之前 | 加载为 terminal；恢复显示不启动重复 run |
| 修复备份、替换或审计过程中 | 原件可取回；不将未完成修复报告成功 |

使用真实子进程、计数文件或受控服务计数副作用；测试断点同步用管道／事件，不靠随机 sleep。
Python 全量及 Host/Web API 回归、schema 兼容 fixture、Atlas/契约生成物检查全部通过后再交付。

## 9. M5 扩展任务质量评测

### 实施

继续使用既有 benchmark 入口。最少加入下列任务类型，任务通过条件来自结果而不是模型自述：

| 类型 | 主要评分 |
|---|---|
| 只读定位和问答 | 必要路径／证据正确，工作区没有 mutation |
| 修复明确失败测试 | 指定测试从失败变通过，改动范围满足约束 |
| 限定重构或局部编辑 | 目标变化正确，非目标文件或 selector 不变 |
| 大输出与长上下文 | 压缩后仍能读取必要 Artifact 并正确完成目标 |
| 人工取消和硬退出 | 中断状态准确，恢复后副作用计数正确 |
| Plan 和审批拒绝 | 被拒绝操作未执行，门禁没有被完成文字绕过 |
| 多 Agent 导入 | 父 dirty path、冲突和失败结果正确阻断，成功导入通过验证 |

确定性契约样本使用 ReplayModelDriver、FunctionModel 和已有 MockTransport。
真实模型质量测试显式 `--execute`，每次新建独立工作区、Session 和配置；固定任务、模型 ID、
reasoning、并发及预算。历史只读任务不被默认赋予写入工具；不同任务使用明确最小工具集合。

评分复用已有 pytest 或已声明的验证命令，在既有执行／Sandbox Seam 中运行，限制超时及输出；
任务文件不得绕过信任策略执行任意验证器。模型提交结果后才跑最终 grader，保存失败原因与有界证据。
任务夹具不包含真实用户数据，公开样本与私有验收样本区分。

报告至少包含：task success、false completion、scope violation、重复副作用、逻辑请求／实际尝试、
首事件／首文字延迟、耗时、已报告 token、cache 与成本、父子分别计量及总量已知程度。
某些 child/cost 缺失时总量不能标为完整；比较样本交错执行，减少顺序和 Provider cache 偏差。

### 验收与推广门禁

- 先建立至少 12 个小任务，覆盖表内类型；每个有独立机器验证条件和可解释失败原因。
- 离线确定性任务在 CI 全部通过；安全越权、重复副作用和 false completion 不允许回归。
- paid 对照至少每个任务 5 次，报告样本数、成功率和分布，不以少量样本宣称统计显著。
- token／时间优化必须同时满足任务质量未见退化；若成功率下降，保留原策略并扩大诊断样本。
- 真实 Provider 数据未获得时，离线交付可完成，但真实质量／性能门禁持续标为待验收。

## 10. M6 优化目录和文本搜索

### 实施

修改 `tools/builtin.py`，复用 `constants.IGNORED_DIRS` 和 Workspace 路径约束。
第一切片仅用标准库，不增依赖，不改变正则语言或现有输出形状。

- `list_directory` 使用深度受限的 walk/scandir；到最大深度就停止递归，逐目录稳定排序，输出
  达字节上限即停止。共享忽略目录用于隐式探索；用户明确指定的允许目录仍可查看，不把忽略
  规则当权限系统。若排序与现有全局排序不同，更新公开行为及兼容测试。
- `_iter_search_files` 不先保存全部 candidates；默认和自定义 glob 均在遍历前剪枝 ignored
  descendants。保留显式单文件检索、glob 语义和相对路径呈现。
- `search_text` 逐行解码匹配，达到匹配／字节预算及时停止，二进制与无效 UTF-8 行为明确。
  超长行不做会漏掉跨块匹配的 naive chunk regex；保留完整行语义或明确拒绝超过受测上限的行，
  提示使用其他有界入口，不能将跳过内容报告为完整无匹配。
- 同步 Python regex 不能假定协程 timeout 能终止工作线程。首期不变更 regex 引擎；增加行大小
  限制并记录剩余风险，确需强取消时用受控进程执行 Seam，不能启动不可回收的线程任务。
- 原有输出 truncation 和错误提示保持可判定，路径／symlink 拒绝不放宽。

仅当标准库切片在 M0 仍不足时增加可选 `rg` fast path：验证实际可执行文件，经过既有异步
进程／Sandbox Seam，处理取消与进程树；Python regex 与 Rust regex 不等价，unsupported pattern
显式回到兼容路径，不返回不同语义的结果。不把 Plan 下的 arbitrary run_command 当 read 搜索。

### 验收

深度检查用目录访问计数证明没有遍历更深层；早期匹配不扫描全树；大文件搜索不整文件读取。
覆盖 custom glob、ignored dirs、单文件、稳定排序、无命中、符号链接、超长行、UTF-8、输出截断
和适用平台路径。执行 builtin、file search、Workspace/Gateway 相关测试，Python 全量回归。

## 11. M7 增加 turn 文件恢复体验

### M7a 变更归属和恢复预览

先建立明确的 turn → 已跟踪 Effect → 文件快照关联。不能按时间戳或文本猜测归属：当前 Effect
模型没有足够 turn 身份时，增加严格的可选 interaction 关联并按实际 schema 升级。
旧 Effect 没有关联保持未知，仅保留现有单 work product 恢复，不伪造历史 turn 可撤销范围。

预览由 TaskWorkspace 生成，Host 只协调和投影。列出：目标 turn、每个文件的当前／目标 revision、
新增／修改／删除、可恢复性、冲突与未覆盖动作，以及预览摘要身份。
同一文件跨多个 turn 修改时，计算目标边界前的确切 snapshot，而不是默认恢复到首次版本。

首期覆盖有 before/after 快照的父工作区文件 mutation。run_command、MCP、外部服务和不能明确
归属的 child 导入不得标为可撤销；支持 child 导入需要其 own Effect 与父导入边界证据。
初始不存在的文件、被删除文件、不可用 Artifact、路径类型变化均单独展示和验证。

### M7b Host 命令和客户端

新增结构化“预览文件恢复”和“应用文件恢复”命令；后者经根 Host 审批及 workspace 独占写锁，
阻止 active run、Live execution、未结束写入和待导入 Agent 并发修改。
TUI/Web 使用同一结果和错误契约，headless 可先支持 JSON 预览；执行仍遵守自身审批模式。

应用时重新核对预览身份、所有文件当前 revision 和安全路径，调用已有
`TaskWorkspace.restore_work_product`。用户预览后修改文件必须使操作失效，不能覆盖用户改动。
复用 prepared/applied/verified/rolled_back/failed，恢复动作自身有记录和可审查 diff。

首期文件恢复与会话分支／消息重生成是两个独立动作，不提供“同时恢复文件和会话”的原子承诺。
多文件先全部 preflight，再逐文件执行；发生中途失败就停止并报告已恢复与剩余文件，不伪装
全局事务或自动覆盖。崩溃后按既有 Effect reconciliation 继续核实。
幂等 request 与恢复结果关联，重复提交不重复变更。

### 验收

- 两个 turn 连续修改同一文件、多个文件、创建与删除、外部修改冲突均恢复到正确边界。
- 未跟踪命令／远端动作和未知历史关联显示未覆盖；所有恢复文件 revision 可验证。
- 中途失败／取消／进程退出可解释且保留 Effect；无越界、symlink 或 Windows 安全边界降级。
- 客户端预览 diff、确认、过期预览、部分成功、键盘操作与焦点恢复通过交互测试。
- 完成 Host/Web API、TUI interaction/snapshot、Web tests/typecheck/build、schema freshness 和
  Python 全量回归；仅预期视觉变化更新 snapshot。

## 12. 质量门禁和生成物

每个 PR 先最小相关回归，再执行仓库规定的扩展检查；通过后不无故重复。
下面的命令是未来实现的门禁，本计划的文档交付不代表它们已经在新实现上执行。

```bash
# Python 静态质量
uv run --frozen ruff check .
uv run --frozen pyright

# 主状态、恢复、Context 或 Agent 生命周期变化
uv run --frozen pytest

# Host / schema / API 变化
uv run --frozen pytest tests/test_workspace_host.py tests/test_web_api.py

# 启用增强网页提取和 Live 的完整回归
uv sync --frozen --all-groups --extra web --extra live
uv run --frozen --extra web --extra live pytest

# Web 使用 CI 的 Node 22 / pnpm 10
pnpm --dir src/web install --frozen-lockfile
pnpm --dir src/web test
pnpm --dir src/web typecheck
pnpm --dir src/web build

# 当前契约与文档生成物
uv run --frozen python -m lumen.contracts --check
uv run --frozen python scripts/export_provider_catalog.py --check
uv run --frozen python scripts/build_architecture_atlas.py --check
pnpm --dir src/web api:schema:check
```

改到生成物事实来源时使用 AGENTS.md 的 write/export 命令更新，不手工编辑。
`api:schema:check` 会再生成并与 Git 比较，已改 schema 的差异需核对；Architecture Atlas 的手写
`index.html` 同步审查。当前 plans 不进入 Atlas，新增本计划不应把它加入当前实现索引。

发布验收还需要先构建 Web，再 `uv build`；隔离安装 wheel，执行 `lumen --version` 和
`lumen --check-config`。真实 Provider 采样与 Linux/Darwin/Windows 的运行时验证分别记录；
Windows 已知限制不能因类型检查通过而标为完整支持。

## 13. 风险与回退

| 风险 | 预防与停止条件 | 回退方式 |
|---|---|---|
| 快速缓存隐藏外部变更或错误 active lineage | 执行准入重验、descriptor 身份校验、严格全量投影对照 | 关闭快速路径并删派生索引，继续严格读取 |
| 线程取消后写入仍执行，第二个 run 提前进入 | 不提前释放锁，写入完成屏障和 shutdown 测试 | 回到原同步路径；不取消已启动写入的事实处理 |
| FTS 工作积压、blob 引用先于耐久落盘 | 有界任务与引用前持久化，降级索引 | 关闭异步索引或清理可重建 FTS，保留 blobs |
| 事件回收破坏审批、SSE 或幂等 | active 引用保护、绝对 sequence、过期 cursor 契约 | 停用回收切片；不删除持久化记录或重执行请求 |
| checkpoint 造成历史／usage 重复或错误 replay | prefix/boundary 校验、真实强杀和副作用计数 | 保留新 schema reader，停止写新 checkpoint，显式处理 interrupted |
| 尾行修复舍弃真实执行证据 | 原件备份、digest 再验、只处理末尾、不猜 Effect | 恢复备份到新诊断副本；不自动重新运行 |
| 文件恢复覆盖用户变更或只完成部分文件 | revision 与路径再验、逐文件 Effect、明确部分成功 | 用户选择有快照的反向恢复，未验证状态保持 reconciliation |
| benchmark 输出看似成功但任务错误 | 独立 grader、false completion 和范围检查 | 不推广新策略，保留失败样本并定位原因 |

schema 增长后的回退是禁用新增写路径而保留兼容 reader，不能降级到不认识该 schema 的程序。
文件恢复不是发布代码回退；每个 PR 的回退说明必须区分程序、派生索引和用户产物。

## 14. 每个切片的交付记录

实施 PR 必须记录具体行为、原因、源码／测试依据和实际验证；填写下表并更新本计划状态。
没有跑过的项保持待验收，不把预期结果写成事实。

| 切片 | 当前状态 | 完成所需证据 |
|---|---|---|
| M0 | 本地完成 | 复现命令、环境及前后各30次原始样本，见验收报告 |
| M1 | 本地完成 | timing 口径与兼容测试、敏感数据检查 |
| M2 | 部分完成，性能门禁未全部通过 | I/O、取消／持久化及全量回归通过；热列表／分页达标，完整读取退化，冷索引未实现 |
| M3 | 替代方案本地完成 | retained bytes、SSE/幂等/审批与客户端通过；临时磁盘回放替代 snapshot/gap，磁盘配额待验收 |
| M4a | 本地完成 | Accepted 14.7、v12 fixture、完整批次与 terminal 去重 |
| M4b | 本地完成，平台门禁待验收 | 六种真实强杀、副作用计数、维修保留／拒绝／故障通过；跨平台与真实断电未验收 |
| M5 | 离线完成，Provider 门禁待验收 | 13个受信入口／28个参数化场景；没有真实 Provider 对照数据 |
| M6 | 本地完成，存在性能退化 | 遍历剪枝、搜索兼容、安全路径通过；小文件无命中场景变慢 |
| M7a | 本地完成 | Effect turn 关联、恢复预览和旧记录降级契约 |
| M7b | 本地完成 | 实际恢复、冲突／部分失败、客户端契约及隔离 wheel 验收 |

本轮已依次实现 M0/M1、M2/M3/M6、M4/M5/M7；当前工作树尚未提交、推送或发布。
推广仍需 Provider、其他操作系统和真实工作负载数据，不能将本机离线结果当作这些门禁已通过。

## 15. 外部参考与采用范围

资料核对日期为 2026-10-09；外部 `main` 与产品文档会变化，实施时记录受测版本／commit。
参考机制不等于照搬其实现；Claude Code 内部实现不作推断。

- [Codex Thread Store](https://github.com/openai/codex/blob/main/codex-rs/thread-store/README.md)：
  canonical history 与可查询 metadata 的边界；Lumen 的派生投影仍由自身 Repository 管理。
- [Codex live writer](https://github.com/openai/codex/blob/main/codex-rs/thread-store/src/local/live_writer.rs)：
  append、persist、flush 与 shutdown；采用持久化屏障思想，不复制 Rust 存储层。
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)：分页、事件生命周期与无订阅者卸载；
  采用客户端契约与生命周期原则，不复制全部协议。
- [Claude Code 运行机制](https://code.claude.com/docs/en/how-claude-code-works)：探索、执行、验证及
  上下文按需加载；Lumen 已有机制继续优化，不作为新增另一 Loop 的理由。
- [Claude Code checkpointing](https://code.claude.com/docs/en/checkpointing)：文件恢复预览与明确覆盖
  边界；Lumen 复用 TaskWorkspace，不承诺恢复命令／外部动作。
- [OpenAI 验证闭环示例](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)：
  用可执行反馈验证输出；落实到现有 benchmark 与测试，不引入新的 Eval 平台。

## 16. 实施结果与剩余门禁

| 阶段 | 已落实 | 明确边界 |
|---|---|---|
| M0 | 30 样本微基准，环境与 lock 摘要、原始耗时／读取／解码／内存／loop lag | 不是真实 Provider 指标；搜索访问和早停另由契约测试计数 |
| M1 | diagnostic v2，逻辑请求、实际 attempt/retry、耗时总量及未知 cache 区分 | 未测区间的 wall time 仍未知；退避仅记录 scheduled seconds |
| M2 | 身份绑定缓存32条／16MiB代理限额、已知追加增量投影、热 overview／分页、嵌套隔离、异步 Context/Artifact/Effect/terminal 屏障、原子 blob、未变 episode 不重写 FTS | 冷列表／分页仍严格构建完整投影；暂未加持久化 offset/catalog 索引，完整 load 复制成本需后续实测优化 |
| M3 | 1MiB事件尾部、完整临时磁盘回放、完成run32条／30分钟、订阅pin、轻量去重身份10000上限、1000run压力契约 | 采用临时磁盘回放替代新snapshot/gap协议；磁盘仍随active流增长，冷cursor一次扫描；幂等仅同Host |
| M4 | schema v12完整批次、增量/边界/重复校验、payload转存、terminal覆盖、维修预览/原件/摘要/审计、六种真实硬退出及维修故障测试 | 不恢复半截输出/Loop游标，不透明重放未知动作；Windows目录持久化与真实磁盘断电需外部验收 |
| M5 | 13类受信离线契约入口，参数化扩展后28个场景；paid入口支持显式read/edit、精确文件/范围评分、false completion、固定种子交错、默认5次 | 离线契约不是模型成功率；未调用付费Provider，成本/缺失用量为未知，不执行任务提供的任意验证器 |
| M6 | 深度剪枝、ignored子树剪枝、流式候选/逐行regex/提前停止，custom rooted glob和64Ki字符单行拒绝 | 无命中小文件可能变慢；未增加rg或正则进程隔离 |
| M7 | Effect.turn归属、精确before快照、digest预览、全文件preflight、明确确认、冲突/待导入阻断、取消结果保存、Host/API/TUI/Web | shell、remote、旧无归属Effect、child导入仅说明排除；多文件非原子事务，部分失败明确报告 |

M2/M3 的实现取舍已记录在 Accepted 14.7：先满足热列表／分页预算，保留一个严格 projector，
不为冷目录再复制一套 lineage；先保持完整 cursor 回放，再用受测磁盘溢出约束内存。
原计划中“冷目录不物化消息”和 snapshot/gap 并未实现，不能按代码已落地声称这些条件已通过。

复现命令（均在仓库根目录）：

```bash
uv run python scripts/benchmark_agent_loop.py --micro --samples 30 --output /tmp/runtime.json
uv run python scripts/benchmark_agent_loop.py --offline --output /tmp/contracts-eval.json
uv run --extra web --extra live pytest
uv run ruff check .
uv run pyright
pnpm --dir src/web test
pnpm --dir src/web typecheck
pnpm --dir src/web build
```

付费矩阵仍需明确配置和任务文件，显式 `--execute --output` 后运行；建议每个任务至少5次，
在同机/同任务/同模型/reasoning/并发条件下比较，达成质量门禁再推广。
基线及最终样本见 [本地验收报告](../research/2026-10-09-runtime-optimization-validation.md)。

## 17. 从旧 Harness 计划迁入的推广门槛

整理日期：2026-10-10。早期 L0–L2 精简计划的持久化、单写者、诊断与恢复意图已由
[单一 Loop 决策](../architecture-guide/13-native-agent-loop-migration.md)、
[长任务恢复](../architecture-guide/14-long-running-recovery.md)及本计划实施结果覆盖，
不再并列维护旧计划。其尚未通过的真实 Provider 门槛并入 M5：

固定模型/版本、协议、endpoint、任务、reasoning 和并发，采集脱敏 conformance、凭据 smoke、
text/thinking/tool delta、usage/cache/private part 及所有终止原因。记录质量、重试、首事件、
TTFT、Loop 本地耗时、费用和父子总量完整度；未知值不填零，区分冷暖缓存。
至少覆盖问答、单文件修复、多源调查、独立多文件与依赖链；只读 benchmark 不替代写入验收。

沿用 Accepted 13.9 的发布预算：排除 Provider/工具耗时后的 Loop p95 增幅 ≤5%，
TTFT median 额外开销 ≤20ms，目标平台取消/超时后不遗留 stream/task/client/进程树。
本机 fixture、构建和文档整理均未补齐这些门槛。失败时停止推广并回退上一发布版本，
不得对已产生 Effect 的活动 run 切换 Loop 或透明重放未知动作。
