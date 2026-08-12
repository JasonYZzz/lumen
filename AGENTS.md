# Lumen Repository Guide

本文件适用于整个仓库。子目录若新增更具体的 `AGENTS.md`，以离目标文件最近的规则为准。

## 项目定位

Lumen 是通用、可配置的 Agent framework。模型 provider 是可替换实现；Lumen 自己拥有 Session、上下文、工具、审批、Sandbox、工作对象和多 Agent 生命周期。

当前 Python 支持范围是 3.11–3.13。发行包名是 `lumen-agent`，Python 包与 CLI 命令均为 `lumen`。

## 事实来源与优先级

1. 源码、契约测试和当前 schema 是运行行为的最高事实来源。
2. `docs/architecture-guide/` 中标记 Accepted 的决策记录描述当前架构和不变量。
3. `README.md`、`docs/commands.md` 描述用户可见行为。
4. `docs/plans/` 是历史计划，不代表功能已经实现。
5. `docs/research/` 是分析材料，不是实现规范；若与 Accepted 决策或源码冲突，不据此改代码。

文档与实现发生漂移时，应在同一修改中更新文档。不要为了让测试通过而保留已被新架构替代的第二套状态权威。

## 核心 deep modules 与依赖方向

- `WorkspaceHost`（`src/lumen/application/host.py`）是 TUI、Web、headless 共享的应用层 seam，拥有 Session actor、run、审批与客户端命令契约。
- `RunCoordinator`（`src/lumen/run_coordinator.py`）拥有单 Session 的 turn、恢复和交互队列协调。
- `AgentRuntime`（`src/lumen/runtime.py`）拥有单 Agent 模型/工具 loop、provider 事件翻译与 completion gate。
- `ContextEngine`（`src/lumen/context/`）是上下文 prepare/commit/control 的唯一 seam；完整历史仍由 Session journal 持久化。
- `TaskWorkspace`（`src/lumen/work_products/`）拥有工作对象、snapshot、effect journal、局部修改、验证和恢复。
- `AgentOrchestrator`（`src/lumen/agents/orchestrator.py`）是 Agent Thread、调度、消息、恢复、证据与完成门禁的唯一权威。
- `AgentRuntimeFactory`（`src/lumen/agents/runtime_factory.py`）创建权限收窄、上下文隔离的 child runtime，并管理 worktree 结果。
- `ToolRegistry`、MCP toolset、Hook 是能力 seam；Skill 是领域流程指令，不拥有运行时状态。
- `SessionRepository`（`src/lumen/sessions.py`）只追加持久事实，不执行调度。

依赖应由客户端 Adapter → Host → Coordinator/Runtime → Context/Tools/Session。TUI 和 Web 不应直接修改 Runtime、Plan、TaskWorkspace 或 AgentOrchestrator 的内部状态。新行为优先扩展已有深 Module 的 Interface，不在每个入口复制逻辑。

使用以下术语：Module、Interface、Implementation、Seam、Adapter、Depth、Leverage、Locality。不要用含糊的“组件/服务/边界”代替它们。

## 设计与清理规则

- 一个状态概念只能有一个运行时权威。兼容层必须是指向新权威的薄 Adapter，不能保留第二套调度、持久化或恢复实现。
- 一个 Adapter 往往意味着假设性的 seam；只有确实存在两个实现或明确替换需求时才增加公开 Interface。
- 对新抽象使用 deletion test：删除后复杂度若消失，它是 pass-through；删除后复杂度会散落到多个调用点，它才有深度。
- 不为“未来可能需要”添加字段、Hook、策略类、转发 Module 或第二套 DTO。持久化审计字段和已批准计划要求除外。
- 不按行数拆分 Module。先减少调用者必须理解的 Interface，再把复杂 Implementation 留在最有 locality 的位置。
- 删除代码前必须给出至少一种证据：零生产引用、被同一权威完全替代、不可达分支、重复实现或契约已删除。框架反射入口不能仅凭文本引用判断。
- Textual action/event/compose 方法、FastAPI route handler、Typer command、Pydantic validator、序列化字段和 Pydantic AI capability callback 可能由框架反射调用；Vulture 的低置信度结果不是删除依据。
- 删除死实现时同步删除只验证该死实现的测试；保留或新增针对公开 Interface/兼容 Adapter 的契约测试。
- 保留当前 dirty worktree 中与任务无关的用户修改，不做 `git reset --hard`、`git checkout --` 或批量覆盖。

## 关键不变量

### Session 与上下文

- Session 是 append-only journal。修改历史事实要追加新 record，不原地改写旧 record。
- 当前 Session schema 是 v8；v1–v7 必须可加载，且加载不能重写历史文件。
- 大正文、transcript、diff 和结果载荷进入内容寻址 ArtifactStore；Session 只保存引用和有界摘要。
- compaction 不能丢弃 canonical history，也不能把瞬时 Skill/Memory/MCP 内容重复写入历史。
- 配置 schema 是 v2。v1 仅在内存中迁移并警告，不自动改写可能含凭据和注释的用户文件。

### 工具、副作用与安全

- `Risk` 决定审批；`EffectKind` 决定状态追踪、并发和验证。不要合并这两个概念。
- 未声明的 MCP/插件副作用默认 `unknown`；未知外部动作不能在恢复时自动重放。
- 文件访问必须经过 workspace 路径解析，拒绝 `..`、绝对路径逃逸和符号链接逃逸。
- `run_command` 只能声明 execution receipt，不能声称完整捕获任意命令产生的文件副作用。
- Sandbox 与审批正交。`workspace_write` 必须 fail closed；角色、Skill、插件或子 Agent不能扩大父级权限。
- 不把 secret、API key、个人数据或完整凭据写入日志、Session、测试 fixture、diff 或 Artifact 摘要。

### TaskWorkspace

- 普通问答不创建 Work Product；正文按需读取，不常驻注入上下文。
- mutation 使用 `prepared → applied → verified/failed → rolled_back` journal。
- 多候选 selector 必须安全失败，不能猜目标。
- 文本/结构化局部修改必须验证目标已变化、非目标区域/路径不变。
- 未验证 mutation 或 strict 模式下未处理的 unknown effect 必须阻止完成声明。

### 多 Agent

- V1 最大深度为一；child runtime 不获得多 Agent 控制工具。
- 子 Agent 工具 = 父轮次有效工具 ∩ 角色 allow-list ∩ workspace mode 允许工具。角色只能收窄能力。
- explorer 共享工作区只读；default/worker 写入独立 Git worktree。
- AgentOrchestrator 是唯一生命周期权威。旧 child 工具和 Host 命令只能通过 `LegacyChildRunAdapter` 投影到原生 Agent Thread。
- Agent ID 必须按父 Session 校验所有权；spawn 重放必须幂等。
- 活动、未送达结果、未处理失败、待审批、待导入、冲突、缺失 evidence 或未验证导入都会阻止根 Agent 完成。
- worktree 导入前检查基线、父工作区 dirty path 和三方冲突；不得覆盖用户未提交修改。
- fork 不复制运行中的执行；未完成 Agent 在新 Session 中记录 `not_carried`。

## 代码规范

- Python 使用 `from __future__ import annotations`、严格 Pyright 类型和 Ruff 规则；行宽 110。
- 对持久化/外部输入使用 Pydantic strict models（通常 `extra="forbid"`）；领域状态优先 `StrEnum`，避免散落的裸字符串。
- 异步路径不得调用阻塞 I/O；确需子进程时使用已有执行/Sandbox seam，并正确处理取消、超时和进程树。
- 接受依赖，不在深 Module 内隐藏创建不可替换的外部依赖；测试和调用者应穿过同一个 Interface。
- 返回结构化结果并追加类型化事件；不要让工具或 UI 直接写 timeline。
- 公共行为变更必须覆盖 Default、Plan、TUI、Web、headless 中受影响的共同契约，避免入口分叉。
- 兼容逻辑要写清弃用对象、权威对象和删除条件；不要用永久性的 `legacy` 分支掩盖新旧双轨。
- 使用 `rg` / `rg --files` 检索。修改文件使用补丁，避免无关格式化和机械性全仓改写。

## 本地开发与验证

在仓库根目录运行：

```bash
uv sync
uv run ruff check .
uv run pyright
uv run pytest
pnpm --dir src/web test
pnpm --dir src/web typecheck
pnpm --dir src/web build
uv build
```

从任意目录安装当前 checkout：

```bash
python3 /absolute/path/to/lumen/scripts/install_editable.py
```

直接执行 `uv tool install --editable .` 时，`.` 必须是 Lumen 仓库根目录。

验证按风险分层：

1. 先运行修改 Module 的最小回归测试。
2. Python 修改至少运行 Ruff、Pyright 和相关 pytest。
3. Host/schema/API 修改运行 `tests/test_workspace_host.py`、`tests/test_web_api.py`，并检查 OpenAPI 生成物。
4. TUI 修改运行相关交互测试与 snapshot；只有预期视觉变化才更新 snapshot。
5. Web 修改运行 Vitest、TypeScript typecheck 和 Next build。
6. 配置、Session、Runtime、TaskWorkspace 或 Agent 生命周期修改完成后运行全量 pytest。
7. 打包/入口修改运行 `uv build`，并从生成 wheel 执行至少一次 `lumen --version` 或 `--check-config`。

`src/web/openapi.json`、`src/web/src/lib/api/schema.generated.ts` 和 `src/web/out/` 是生成物。只通过对应生成/构建命令更新，不手工编辑；提交前确认生成物与源 schema 一致。

## 变更交付检查

- 说明根因或设计依据，不只列修改步骤。
- 列出删除内容及“为什么可删”的生产引用/权威证据。
- 明确保留了哪些兼容、安全和恢复路径。
- 报告实际运行的测试、静态检查与构建结果；不要把“工具调用成功”描述成任务验证成功。
- 不在未经用户授权时提交、推送、发布或覆盖外部工作区。
