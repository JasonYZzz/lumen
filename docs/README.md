# Lumen 文档地图

本目录按“当前事实”和“时间点证据”分层。阅读或修改架构时，优先级始终是源码、契约测试与当前
schema，其次是 Accepted 决策记录；计划和研究不能反向定义运行行为。

## 当前事实

- [`architecture-guide/`](architecture-guide/README.md)：当前架构、运行流程、状态权威与 Accepted 决策。
  网页版入口是 [`architecture-guide/index.html`](architecture-guide/index.html)。
- [长任务持续执行与排障](architecture-guide/14-long-running-recovery.md)：超时、重试、同轮压缩和恢复的当前契约；维护记忆同时保存在根 `AGENTS.md`。
- [`brand/`](brand/README.md)：Lumen Fold 标志、横向组合、色彩与生产使用约束。
- [`commands.md`](commands.md)：CLI、TUI 与 Web 可见命令行为。
- [`skills/authoring.md`](skills/authoring.md)：Skill 作者契约。
- [`hooks/README.md`](hooks/README.md)：Hook 接入与安全约束。
- [`generated/contracts.json`](generated/contracts.json)：由源码生成的契约目录；不能手工修改。

## 时间点证据

- [`plans/`](plans/README.md)：仍有未完成发布验证或条件式后续工作的活动计划。计划不等于已实现。
- [`research/`](research/README.md)：固定日期、版本或 commit 的外部研究快照。结论可能被后续实现取代。
- [`spikes/`](spikes/langgraph-context-resume.md)：隔离技术验证，不属于生产依赖。

## 维护规则

1. 公共行为变化必须同时更新源码、契约测试以及受影响的当前文档。
2. Accepted 决策被替代时新增或更新决策记录，并明确旧决策的状态；不要让两套文档同时声称权威。
3. 研究必须保留日期和证据版本，不改写成“当前实现”。有未完成门禁的计划继续保留；被同一权威
   完全替代的旧计划在迁移仍有效的验证要求后删除，同时更新索引和引用。
4. 无用文件只有在确认零生产引用、被同一权威完全替代或目录已不再需要占位后才删除；独立的
   历史证据和兼容契约不因日期较早而失效。删除依据记录在当前实现审计中。
5. Architecture Atlas 的 Trace Reader 只索引当前事实、源码、测试和生成契约，不索引 research、plans、
   spikes。

刷新并验证网页版证据快照：

```bash
uv run python scripts/build_architecture_atlas.py
uv run python scripts/build_architecture_atlas.py --check
```
