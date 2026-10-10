# 运行时优化本地验收记录

> 文档复核（2026-10-10）：本文保留原始研究／验收证据；当前实现见[文档地图](../README.md#当前事实)。外部资料与测试结果仍以本文原日期、版本和环境为限。

日期：2026-10-09。测量源码基线 `291348aa956c88e6ccd6f465fd4ea63656956e7c`；当时优化位于未提交工作树。
后续代码纳入 `7f5e5ce`，Web 恢复入口纳入 `fa0aba3`；下列测试与样本仍对应原测量环境。
范围及偏差见[实施计划](../plans/2026-10-09-runtime-optimization-implementation-plan.md#16-实施结果与剩余门禁)，
当前持久化与回放取舍见 [Accepted 14.7](../architecture-guide/14-long-running-recovery.md#147-完整批次耐久与文件恢复决策)。

## 已验证的行为

- 热 Session 不重读 JSONL，已知追加只解码新增记录；相同大小／保留 mtime 的外部修改、替换、
  缩短、损坏记录和 strict admission 都回到严格投影。返回值的嵌套消息、计划及使用量不污染缓存。
- Context、Artifact、原生 Effect recorder、完整批次和 terminal 使用等待实际线程结束的边界。
  terminal fsync 期间取消仍只发布已耐久的完成结果，不产生 Session 完成、Host 取消的矛盾。
- 完整批次只构成一条 running turn，terminal 校验前缀后覆盖；v1–v11 兼容升级保持原字节。
  大 checkpoint 先发布内容寻址 blob，缺失／篡改拒绝；不保存 token delta 或未提交 summary parent。
- 六种管道同步的子进程硬退出：请求前、半截参数、未知外部执行中、部分并行工具、完整批次
  fsync 后、terminal fsync 后。半截参数零执行，未知结果阻止重试，已确认成功动作计数不增加。
- 尾行维修要求确认与摘要复核，完整原件保留；备份／替换／审计故障不报告成功，合法原件可取回。
- 1000 完成 run 后重对象保持上限，慢 cursor 完整重放、断线不取消执行、迟到身份不重复执行；
  live 尾部与磁盘回放使用相同 canonical JSON，订阅者修改不能污染另一订阅。
- 文件恢复复用快照和 revision：预览后外部修改拒绝、取消等待实际恢复结束、幂等结果保留。
  Root Host/API、TUI F 键和 Web 分别验证预览后才明确确认；待导入 Agent 阻止应用。
- 深度限制实际剪枝；检索逐行、提前停止，custom glob／ignored／symlink／长行边界保持可判定。

## 本机质量门禁

| 检查 | 实际结果 |
|---|---|
| `uv run --extra web --extra live pytest` | 1527 passed、1 skipped；60 个 TUI snapshots passed |
| Ruff / strict Pyright | 通过 |
| Web test / typecheck / build | 218 passed；类型与生产构建通过 |
| 离线 benchmark 受信场景 | 13 个入口、28 个参数化场景通过；无 Provider 请求 |
| contracts / Provider catalog / Atlas | 通过新鲜度检查 |
| OpenAPI / TypeScript | 已按命令生成；再次生成哈希不变，新增维修／恢复接口差异已检查 |
| `uv build` / 隔离 wheel 安装 | sdist、wheel 成功；锁定基础依赖的独立环境 `lumen --version`、`--check-config` 成功；包含62个静态 Web 文件 |
| `git diff --check` | 通过；没有 commit、push、publish |

`api:schema:check` 按 Git 基线比较，会因本轮生成的预期新增接口返回非零；不是额外生成漂移。
本机 Python 3.12、Node 24 / pnpm 12，不代表 CI 的 Linux/Windows 或 Node 22 / pnpm 10。
Fixture、MockTransport、构建和离线契约不是真实 Provider 或跨平台性能验收。

## 性能样本与限制

原始样本位于 `runtime-optimization-2026-10-09/`：before、after 各30次，另外保存离线场景结果。
样本只有合成夹具、摘要和计数，不含真实会话、正文、凭据或用户 HOME 数据。基线是上述 Git HEAD；
after 另外记录 Python 源码树与测量脚本 SHA256，以区分同一 HEAD 下的未提交工作树。
预热不计入样本，峰值 Python 内存另行测量；p95 为排序后 ceil(n×.95) 的样本值。
这些微基准不是统计显著性、真实任务成功率或端到端延迟承诺。

复现：

```bash
uv run python scripts/benchmark_agent_loop.py --micro --samples 30 --output /tmp/runtime.json
uv run python scripts/benchmark_agent_loop.py --offline --output /tmp/eval.json
```

2000 turn 合成 Session，耗时单位为毫秒；倍数为 after / before，小于1表示降低：

| 场景 | before median / p95 | after median / p95 | median / p95 倍数 |
|---|---:|---:|---:|
| 热列表 | 0.858 / 1.160 | 0.029 / 0.039 | 0.034 / 0.034 |
| 热分页20条 | 0.827 / 0.970 | 0.357 / 0.401 | 0.432 / 0.413 |
| 热完整 load | 0.855 / 1.059 | 82.125 / 101.463 | 96.02 / 95.85 |
| 冷完整 load | 72.276 / 89.048 | 160.865 / 213.891 | 2.23 / 2.40 |
| 已知追加后完整 load | 81.781 / 100.657 | 83.434 / 92.146 | 1.02 / 0.92 |

热列表／分页达到原计划耗时降至基线50%以下的本机场景门禁；热读取和已知追加后读取的
journal read bytes 为0，冷完整读取为2,477,067。完整 load 新增嵌套防御复制，基线浅复制可
污染缓存；此保证变化带来明显退化，冷读取也仍构建所有消息。完整 load 的峰值 Python 分配
由2,559,079增至11,885,560字节，冷完整读取由26,144,134增至28,593,159字节。
不能把热列表／分页的加速描述成全部读取变快，也不能宣布 M2 性能目标全部通过。

| 其他场景 | before | after | 结果 |
|---|---:|---:|---|
| 10MiB Artifact + 索引，loop lag p95 | 270.810 ms | 1.042 ms | 降至0.38%；保存耗时 median 为248.367→244.177 ms |
| 深度1遍历 median | 11.370 ms | 3.540 ms | 降至31.1% |
| 检索早期命中 median | 5.026 ms | 0.469 ms | 降至9.3% |
| 小文件无命中 median | 10.444 ms | 12.646 ms | 增至121.1%，退化 |
| 发布10000事件后驻留 payload | 10,120,000 bytes / 10000条 | 907,764 bytes / 897条 | 完整事件另存临时磁盘；1MiB限制按编码后的完整 envelope 计量 |

检索微基准的计数器不衡量实际文件读取字节，遍历剪枝与早停另由契约测试计数；不能据此
声称读取量的百分比降幅。事件数字仅为 payload，不能当作完整 envelope 或 Python heap 大小。
原始结果：[before](runtime-optimization-2026-10-09/before.json)、
[after](runtime-optimization-2026-10-09/after.json)、
[离线场景](runtime-optimization-2026-10-09/offline-eval.json)。

最终样本环境为 macOS26.5.1 arm64、Python3.12.14、SQLite3.53.1、Node24.19.0、pnpm12.3.4。
Python 源码树 SHA256：`0dba60fcd53eebbbdba5b303ef4f353848d9048ba020550b48aeeaa27802c052`；
测量脚本 SHA256：`cd40f78cbc2175b2df0cb3718e75553cc2282a4489162378b11d4a913027f529`。

临时事件回放约束内存，不约束 active run 的磁盘总量；冷 cursor 有一次扫描成本。
Session 缓存的16MiB是 journal 大小代理，不能当作精确 RSS 上限。
待推广门禁：真实 Provider 每任务至少5次、真实工作负载冷加载预算、Windows持久化/断电场景、
Linux/Windows及CI运行时、临时回放磁盘配额与冷索引是否值得引入。当前没有这些验收数据。
