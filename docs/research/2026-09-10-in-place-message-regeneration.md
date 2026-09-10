# 同 Session 消息重生成与活动历史审计

日期：2026-09-10

## 结论

Lumen 原 Web 实现把“编辑消息”和“创建 checkpoint 分支”复用为同一个
`ForkSessionAtTurn(include_turn=False)` 操作。这能正确复制前缀，却必然产生新的 Session ID、标题和
列表项；UI 还禁止原文不变时提交。更隐蔽的问题是：即使新分支的 turn 前缀正确，旧 Session 的
compaction episode、resource snapshot 或已经学习的 memory 仍可能成为旧事实的第二条召回路径。

本次把两个意图拆成独立 Interface：

- checkpoint 分支继续创建新 Session；
- 消息重新生成通过 `StartRun.regenerate_from_turn` 在当前 Session 追加 `history_rewind`，然后原子受理
  replacement run。Session ID、标题和当前窗口不变；相同文字允许提交。

## 上游真实语义

Pi 当前 Session v3 是 append-only JSONL tree。每个 entry 带 `id/parentId`，leaf 是活动位置；把 leaf
移回早期 entry 后继续 append 会形成同文件新分支，`buildSessionContext()` 只沿 leaf→root 的路径构造
模型消息。它同时把 `/fork` 定义为创建新文件，因此“同文件 tree navigation”和“新 Session fork”是
不同操作。这一结构直接说明：保留原始事实和排除旧模型上下文并不矛盾。

- [Pi Session 文件格式](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
- [Pi SessionManager 源码](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/session-manager.ts)

Codex App Server 同样区分 `thread/fork` 和 rollback/revert 语义：fork 返回新 thread ID；公开的
`thread/rollback` 会从内存上下文删除最近 N 个 turn，并在 rollout 持久化 rollback marker，使 resume
重建相同的裁剪历史。core reconstruction 从后向前按真实 user-turn segment 应用 marker，而不是删除
rollout 行。Codex TUI 的 prompt edit 当前也公开了 fork 事件，说明不同客户端可以选择新 thread 或
同 thread 回退，但两者不应被一个含糊 API 混用。

- [Codex App Server 协议](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Codex rollback handler](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/handlers.rs)
- [Codex rollout reconstruction](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/rollout_reconstruction.rs)
- [Codex TUI prompt-edit event](https://github.com/openai/codex/blob/main/codex-rs/tui/src/app_event.rs)

## Lumen Implementation

`history_rewind.before_turn` 使用 marker 写入时的**活动 turn ordinal**。Repository 顺序 replay JSONL；遇到
marker 后截断当前活动 turns 并从保留前缀重建 active history、full history、compaction parent/range 和
Plan 投影。后续 turn 继续追加，因此多次编辑仍可确定性冷启动恢复。原 turn、工具 transcript、usage 和
receipt 没有被覆写或删除。

Host 在同一个 workspace execution lock 内完成以下 admission：验证 Session/turn、检查未对账 external
effect、追加 rewind、清除 ContextEngine 的 pending/committed/checkpoint/background cache、失效仅由废弃
后缀支持的自动记忆，然后追加 replacement running turn。这样避免“marker 已写但另一个 run 抢先启动”，
也避免相同 prompt 命中旧 context commit 幂等缓存。

`SessionRepository.load_turn_page()` 和 checkpoint episode retrieval 改为消费同一个活动 lineage
projection，删除了 UI 分页和模型 retrieval 各自扫描全量 JSONL 的第二套分支判断。Session resource
snapshot 在 rewind 后清空；active Skill 和 Session 设置保留。工作区文件、Work Product 和已发生的
外部副作用不回滚，未核实 effect 仍会阻止重新生成。

## 风险与验证重点

- 旧 v1–v10 文件保持只读加载；首次使用新 record 时仅追加 v11 `schema_upgrade`。
- 标题 `session_catalog` 不回退，防止编辑第一条消息重新触发标题生成。
- 自动学习只 expire 单一来源为当前 Session、且全部证据 turn 位于废弃后缀的非显式记忆；显式记忆、
  保留前缀证据或其他 Session 共同支持的事实不受影响。
- 未验证外部动作不会因 rewind 被遗忘或自动重放；文件状态继续由 TaskWorkspace 权威管理。
- 冷加载、分页、Default/Plan、Web API、相同文本重发、附件和 UI 不跳转都需要回归覆盖。
