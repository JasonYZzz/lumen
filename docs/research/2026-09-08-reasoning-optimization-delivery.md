# 推理强度与默认并发优化交付

> 后续复核发现兼容模型能力、旧 Session 能力刷新和 Web 设置候选项仍有缺口。
> 本文保留第一轮交付记录；修正及逐项验收状态以[闭环审计](2026-09-08-reasoning-control-closure.md)为准。

本次实现依据[源码研究](2026-09-08-reasoning-and-agent-parallelism.md)。历史报告中的
“未实现”描述保留为当时基线；当前行为以源码和架构第 7、10 章为准。

已实现统一档位与能力校验、CLI/TUI/Web 的 Host 命令、按 Session/模型恢复、角色覆盖和
child 推理参数冻结；模型配置仍为 v2，Session 升至 v10 且只追加升级记录。
默认安全工具并发、子 Agent 默认 3 并发，独立任务先派发后等待；Git 改用现有可取消异步
执行 Seam，并维持导入锁、隔离、审批与完成门禁。没有新增 Loop 或第二套 Agent 调度器。

删除了 Factory 的阻塞 Git 执行正文和单独追加 `thinking` 的角色覆盖逻辑：前者已由工具
命令共享执行 Seam 完整替代，后者已由统一解析权威替代，避免原生参数掩盖角色档位。
旧 raw settings、旧 Session、旧 child 快照和 LegacyChildRunAdapter 仍是兼容路径。

## 可复现实验入口

`scripts/benchmark_agent_loop.py` 默认只校验并显示矩阵，不连接模型。显式 `--execute`
才按同一模型比较 provider_default / low / medium / high 与并发 1 / 3，默认重复 3 次。
每次从 JSON fixture 创建全新临时工作区与 Session，关闭 MCP、Hook、Skill、Memory recall
和学习，只保留文件读取/搜索与只读 explorer，防止其他配置污染实验。使用正常 Lumen Host
和 Provider，不模拟加速结果；正常 ArtifactStore 等应用存储行为仍适用。

任务文件示例：

```json
[
  {
    "id": "independent-lookup",
    "prompt": "分别委派两个 explorer 调查 a.txt 和 b.txt，先派发再等待，汇总两个值。",
    "files": {"a.txt": "alpha=17", "b.txt": "beta=29"},
    "expected_substrings": ["17", "29"]
  }
]
```

```bash
uv run python scripts/benchmark_agent_loop.py --config agent.yaml --tasks tasks.json
uv run python scripts/benchmark_agent_loop.py --config agent.yaml --tasks tasks.json \
  --execute --output results.json
```

结果包含父 Agent 请求/控制批次比例、请求耗时、首语义事件延迟、thinking 字符数、重试、
父/子 usage、Agent 峰值并发及适用的 worktree/排队指标。未提供的 token 明细或费用为未知，
不估算成零。substring 检查只是粗粒度质量代理；复杂修复、写入验收、返工与费用需要另配
真实任务及独立质量评审，不能从这个只读实验外推。

本轮只执行离线 HTTPMock、真实 Orchestrator、临时 Git 和 Host 回归，不访问付费模型。
因此交付结论是参数与并发语义已验证，不能宣称 medium 必然提速或给出真实任务加速百分比。

## 验证记录

- 全量 Python：1113 个测试通过，60 个 TUI snapshot 通过。新增 `/thinking` 改变菜单长度，
  仅更新 4 个菜单 snapshot 的滚动条位置；档位交互另有 Host/TUI/Web 回归。
- Pyright：0 errors / 0 warnings。Ruff 在排除任务前已存在的 `scripts/chart_geometry.py` 后通过；
  全仓命令仍报告该无关脚本的 44 条既有问题，本轮未修改它。
- Web：144 个 Vitest 测试通过，TypeScript typecheck、Next production build 通过。
- OpenAPI/TypeScript 生成物已重建；Python contracts 与架构 Atlas freshness check 通过。
- `uv build` 通过；在独立环境从生成 wheel 执行 `lumen --version`，返回 `lumen 0.1.0`。
- benchmark 默认 24 次比较矩阵的 dry-run 通过；模拟模型穿过真实 Host 的离线实验通过，
  没有执行付费模型性能实验。
