# AgentLoop 优化闭环审计与推理控制修正

> 文档复核（2026-10-10）：本文保留原始研究／验收证据；当前实现见[文档地图](../README.md#当前事实)。外部资料与测试结果仍以本文原日期、版本和环境为限。

日期：2026-09-08。复核对象为当日 checkout 源码和契约测试；不重写用户模型配置、不访问付费模型。

后续修订：供应商匹配已改为经过官方文档核对的精确目录，取代本文阶段的 SDK 能力推断。
当前规则以 [Accepted 决策 15](../architecture-guide/15-provider-catalog.md)及
[自动生成的能力表](../generated/provider-reasoning.md)为准。

## 结论

前一轮“统一推理控制已经完成”的描述不够准确：通路已有，但本项目实际使用的兼容模型没有完整
能力映射，旧 Session 还可能缓存单一选项，Web 设置页也自行枚举通用档位。因此用户看到的
“只有 Provider 默认”确实是实现缺口。此次补齐这些路径；用户可以自行选择，且能看见实际映射。

已确定需要实施的 Loop、推理控制和并发改动形成代码与契约验证闭环。**所有研究建议并非已经
完成实证验收**：真实模型配对实验、产物正确率及速度/成本比较仍未执行。不能据此宣称某档位
必然提速、默认并发 3 必然优于 1，或 Lumen 已比 pi 更快。

## 能力目录的权威

本记录的阶段性模型映射表已由 Accepted 第 15 章与自动生成能力目录替代；不再复制一份可能
漂移的列表。provider_default 表示不指定应用档位，未知部署不能由兼容协议前缀推断能力。
HTTP MockTransport 验证客户端序列化，不证明服务端内部预算或真实模型性能。

## 控制与恢复如何闭环

1. `reasoning.py` 是能力与参数解析的唯一 Module：requested → effective → 类型化 parameters；
   capability_status/source、level_map 向所有 Adapter 提供同一事实。
2. CLI `--thinking`、TUI `/thinking`、Web 选择器经过 Host 更新 Session。运行中拒绝修改；
   下次 Run 重新校验后冻结。配置默认与 Session 临时选择是不同作用范围。
3. Session 按逻辑模型名与模型 ID 保存选择；重开、切换模型、fork 保留各自偏好。
   恢复时刷新能力元数据，不让历史单选列表遮蔽升级后的能力；历史文件仍 append-only。
4. 子 Agent 默认继承同模型父级选择，角色显式选择覆盖；角色切换模型则按目标模型重算。
   已派发 child 保存解析后的类型化参数，不随之后的父级切换而漂移。
5. 同层 YAML 同时设置 reasoning_effort 与原始推理字段时报错。更高层的 CLI/Session/角色选择
   清理更低层原生字段及 extra_body 中已知的冲突控制，保留无关字段；不让旧 high 覆盖新 low。
6. Web 模型设置通过 `InspectReasoning` 查询编辑中的定义；请求仅本地解析，不写配置、不调用模型。
   延迟返回的旧模型能力不覆盖新草稿。输入框选择器禁用未知/不支持的单一默认选项并说明原因。
7. 请求诊断记录实际客户端参数及映射；HTTP MockTransport 穿过真实 SDK 验证序列化。
   这不等于服务端内部推理预算回显，也不等于已完成真实性能实验。

## 原建议逐项核对

| 建议 | 当前状态与证据 | 验收限制 |
| --- | --- | --- |
| 减少计划/进度的纯控制往返 | 已实现；runtime 工具说明、update_step 合批、tests/test_task_execution_regressions.py | 离线模型轨迹验证合批，真实模型采用率待测 |
| 尽早形成产物、局部改进 | 已写入大产物行为说明；已有 TaskWorkspace 局部修改与验证 | 属于行为引导，不能保证所有任务都早写文件；真实质量待测 |
| evidence 与完成门禁 | 保留 TaskWorkspace/AgentOrchestrator 唯一权威；有关联 evidence、未验证产物拒绝完成的契约测试 | 不通过去掉验证换取表面速度 |
| 请求、工具、审批与界面时钟分开 | 已实现 request_observed、工具执行/审批耗时、Context 准备/压缩、worktree 耗时；Loop/Runtime/Gateway 测试 | Provider 未返回的 reasoning tokens、费用是未知值 |
| 单工具完成立即可见，历史有序 | 已实现；test_tool_completion_is_visible_before_the_next_result_and_history_stays_ordered 及取消回归 | 不改变模型历史批次顺序 |
| safe 工具默认并发 | 已实现 parallel_safe；测试验证独立调用重叠、exclusive 屏障、取消后保留已完成事实 | 写入冲突、未知副作用、审批及依赖仍受约束 |
| 统一推理档位/能力校验/参数优先级 | 此次补全实际兼容模型、别名和原生 HTTP 参数；tests/test_reasoning.py | 未知部署需明确能力声明；不伪装成全模型支持 |
| CLI/TUI/Web、空闲切换、Session 恢复 | 已实现并修复旧能力缓存、Web 设置候选源；Host/API/TUI/Web 契约测试 | 模型配置写入后沿用既有重启契约；Session 选择下次 Run 生效 |
| 子 Agent 继承、角色覆盖、冻结恢复 | 已实现；factory snapshot、Session/schema/fork 与恢复测试 | 旧快照兼容；不持久化任意 settings 或凭据 |
| 合理默认、不扩大 max_tokens | 新模板已知推理模型 medium，旧配置保留默认；预算校验测试 | DeepSeek medium 实际 high，因此不能当成比 high 更低 |
| 默认子 Agent 并发 3、先派发后等待 | 已实现同一个 Orchestrator；真实调度测试验证三个 running、第四 queued、spawn 幂等 | 是否拆分仍由任务需求和模型决定，不强行每轮拆三份 |
| 异步 worktree Git、取消、串行导入 | 已实现；tests/test_agent_execution.py 验证事件循环、子进程树清理、dirty 导入拒绝和恢复 | explorer 只读；worker/default 独立 worktree；权限不扩大 |
| 新批量 spawn Interface | 条件建议，当前不新增 | 现有顺序 spawn 已真实并发；无真实轨迹证据需要第二入口 |
| 本地热点缓存、更激进停滞策略 | 条件建议，当前不新增 | 先做 profile；已有 stall 证据和安全恢复，不用更短硬墙代替性能分析 |
| effort × 并发的配对性能实验 | 只完成可执行的只读 benchmark 入口、dry-run、真实 Host 的离线测试 | **未完成真实模型执行、写入任务质量矩阵、费用/返工对比及 pi 配对实测** |

真实实验下一步应固定模型/版本/端点/输入和验收标准，覆盖问答、单文件修复、多源调查、
独立多文件任务和依赖链；比较有效档位而非把别名当不同强度，重复运行并区分缓存。
现有 scripts/benchmark_agent_loop.py 仅面向只读 fixtures，substring 命中不是交付质量验证，
不能将它描述为已经覆盖全部实验建议。

## 清理、安全和验证

删除/替代的逻辑：去掉 Web 的固定通用档位枚举（被 Host 能力查询完全替代）；
去掉“所有 Anthropic-compatible 模型都按 Claude 推理”的推断（兼容路由有不同原生契约）；
旧 Session 的 supported_levels 不再是当前能力权威（用户选择与能力事实必须分开）。
没有新增第二套 Agent 队列、重试循环或 Session 状态，也未删除历史兼容记录。

保留原生 Loop 单一重试权威、未知外部动作不自动重放、append-only Session、v1–v9 读取、
child 权限交集、worktree dirty/conflict 检查、审批与 Sandbox 正交、完成门禁和 reasoning 原文保真。

本轮实际验证结果：

- 全量 `uv run pytest`：1174 passed，60 个 TUI snapshot passed。
- `uv run pyright`：0 errors / 0 warnings。
- `uv run ruff check . --exclude scripts/chart_geometry.py`：通过。全仓 Ruff 仍有该无关、
  既有脚本的 44 条问题（43 RUF001、1 E501）；本次未改动该脚本，不能宣称全仓 Ruff 全绿。
- Web：148 个 Vitest 测试通过；TypeScript typecheck、Next production build 通过。
- OpenAPI 和 TypeScript schema 已按命令重建；contracts 和 Atlas 生成及 freshness check 通过；
  Atlas 首页手写说明同步更新；git diff --check 通过。
- `uv build` 通过；使用生成 wheel 的独立环境执行 `lumen --version`，返回 `lumen 0.1.0`。
- 对当前项目配置做只读能力解析：DeepSeek V4 Flash/Pro、Kimi K3、Qwen 3.8 Max/Flash
  均返回 supported 和多个档位；只输出模型名、支持集及映射，未输出配置凭据、未发起模型请求。
- 真实 Provider 的参数接受、内部预算、速度、费用和产物质量尚未经本轮联网调用验证；
  新增的请求级测试使用真实 SDK 和 MockTransport，不能替代真实模型实验。

## 合并的原始 Loop 与并发证据（2026-09-08）

整理日期：2026-10-10。以下合并首轮 Loop 分析、推理/并发研究和交付记录的独立证据；
不把优化前缺口或初轮能力推断当成现行实现。

最初比较的 Lumen 基线为 250adf89903761260413577848519e818b825f16 加当时未提交修改；
Pi 为 b2602be77cb7b0de45dd616407fd210daa48aa75，工作区干净。
历史 HTML 报告任务有 23 次逻辑请求/23 次尝试、22 个完成批次、31 次工具调用，
其中控制调用 16 次、纯控制批次 11 个、控制错误 7 次；thinking 共 176,424 字符，
输出 usage 67,025 tokens。累计输入/缓存读取 usage 为 2,030,976/1,829,248。
末段请求间隔约 849 秒，包含生成、工具和上下文准备，不能全部归为模型计算。

案例支持减少无工作产出的控制往返；没有 completion retry/compaction 事件证据，
不能归因为完成门禁循环、压缩风暴或 Python 本地 CPU。thinking 字符不是 token，
输出预留不是输入 prompt，elapsed 工具总和包含等待；未提供完整时钟的失败原因仍未知。

受控 FunctionModel 穿过真实 Runtime、TaskController 与 Gateway，均只执行一次 inspect_report：

| 场景 | 模型请求 | 全部工具调用 | 结果 |
| --- | ---: | ---: | --- |
| 无计划 | 2 | 1 | completed |
| 两步骤计划，控制逐条发送 | 8 | 7 | completed |
| 相同计划与验收，已知参数合批 | 3 | 7 | completed |

合批保留依赖、真实 receipt ID 和完成检查：先 set_plan/开始步骤/实际检查，
收到真实证据后才 link_evidence/更新步骤，再最终回答。不需要新增批处理权威，
不证明真实模型必然采用该轨迹，也不是 Lumen/Pi 性能 A/B。初轮相关定向回归 72 项通过。

原生并发实验使用真实 Orchestrator、临时 Session 与模拟 Factory：依次 spawn 四次，
前三个 running、第四 queued，释放后全部 completed，峰值 3。控制工具顺序登记并不等待
child 执行结束；模型 spawn 后立即逐个 wait 仍会削弱收益。Pi 工具批次默认并行不等于
默认自动拆分子 Agent，adaptive 名称也不是依赖分析算法。

首轮交付的 Python 1113 tests/60 snapshots、Web 144 tests，后来闭环修正的
1174/148 计数见上文；均不是本次整理重跑。两轮当时的全仓 Ruff 均受既有脚本问题限制，
不能改写为历史全绿。

## 保留的实验入口与推广限制

scripts/benchmark_agent_loop.py 默认只显示矩阵；显式 --execute 才调用 Provider。
每个 JSON fixture 新建临时工作区/Session，关闭 MCP、Hook、Skill、Memory recall/学习，
只保留只读文件查询与 explorer；比较 provider_default/low/medium/high 与并发 1/3，
默认重复 3 次。先比较有效映射，不把别名当独立强度。

```bash
uv run python scripts/benchmark_agent_loop.py --config agent.yaml --tasks tasks.json
uv run python scripts/benchmark_agent_loop.py --config agent.yaml --tasks tasks.json \
  --execute --output results.json
```

任务包含 id、prompt、files、expected_substrings；结果记录请求/控制比例、耗时、首语义事件、
thinking 长度、重试、父子 usage、峰值并发及 worktree/排队指标。
未提供的费用/token 明细为未知，substring 是粗粒度只读质量代理。
已验证默认 24 次矩阵 dry-run 与离线 Host 链路，没有执行付费模型 A/B。

写入正确率、返工、费用及同任务 Pi 配对实验仍需独立任务和验收；
发布推广门槛统一跟踪于[运行时优化计划](../plans/2026-10-09-runtime-optimization-implementation-plan.md)。
完整前序记录可从[整理前 Git 快照](https://github.com/JasonYZzz/lumen/tree/8940f92b3a61b0f0e3d7dc0e770da7d06facc3ea/docs/research)追溯。
