# Lumen Repository Guide

适用于全仓库；子目录规则就近生效。下文审批、Plan、多 Agent 约束描述 **Lumen 产品运行时**，不代表开发助手当前的工具或权限。

## 工作约定

Lumen 是通用、可配置的 Agent framework：Provider 可替换，运行时语义由 Lumen 拥有。发行包 `lumen-agent`，Python 包与 CLI `lumen`；Python 3.11–3.13，以 `pyproject.toml` 为准。

- 完成请求范围内的实现、文档和验证，修复本次改动造成的失败；本地编辑、生成物更新、相关测试无需逐步确认。缺少关键决策或权限时说明阻塞，继续可独立完成的部分。
- 保留无关的 dirty/untracked 文件。使用补丁修改；未经授权不提交、推送、发布或覆盖外部工作区。
- 用 `rg` 按任务检索源码和测试。源码、契约测试、schema 定义运行行为；Accepted 架构决策定义意图，README/commands 描述用户行为，plans/research 不证明实现。改动同步更新受影响文档。
- 本文件只保留项目约束、定位入口和完成条件；专项细节按需加载，新增规则需有失败或契约依据。维护原则参考 [OpenAI 官方文章](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)。

## 架构与源码入口

依赖主线：客户端 Adapter → `WorkspaceHost` → `RunCoordinator` → `AgentRuntime` → Context / Loop / Gateway。客户端通过 Host command/event Interface 工作，不直接修改 Runtime、Plan、TaskWorkspace 或 AgentOrchestrator 内部状态。

| 修改涉及 | 权威与入口 |
|---|---|
| Session actor、run、审批、客户端契约 | `application/host.py`：WorkspaceHost |
| 单 Session turn、恢复、交互队列 | `run_coordinator.py`：RunCoordinator |
| turn 外壳、Context prepare/commit、公开事件、partial outcome | `runtime.py`：AgentRuntime |
| 主模型请求、工具批次、重试、取消、终止候选 | `agent_loop/loop.py`：LumenAgentLoop；`completion.py` 提供完成判定 |
| Provider 翻译与资源生命周期 | `agent_loop/driver.py`：ModelDriver；`agent_loop/pydantic_driver.py`：低层 PydanticAI Adapter |
| 工具校验、审批、Hook、幂等、Effect | `tools/gateway.py`：CapabilityGateway；声明/注册在 `tools/spec.py`、`tools/registry.py` |
| 上下文与压缩 | `context/engine.py`：ContextEngine |
| canonical history 与大载荷 | `sessions.py`：SessionRepository；`context/artifacts.py`：ArtifactStore |
| 工作对象、snapshot、mutation、验证和恢复 | `work_products/workspace.py`：TaskWorkspace |
| Agent 生命周期与隔离执行 | `agents/orchestrator.py`：AgentOrchestrator；`agents/runtime_factory.py`：NativeAgentRuntimeFactory |
| 装配与释放 | `resources.py`、`lifecycle.py`：RegistrationScope 只拥有注册和后台任务生命周期 |

表中 Python 路径相对于 `src/lumen/`。更细的问题定位见 [源码导航](docs/architecture-guide/08-code-navigation.md)。

按改动查阅，无需整套加载：

- Loop、Provider、完成和重试：[单一 Loop 权威](docs/architecture-guide/13-native-agent-loop-migration.md)、[长任务恢复](docs/architecture-guide/14-long-running-recovery.md)。
- 工具、Sandbox、MCP、Skill：[工具与安全](docs/architecture-guide/04-tools-permissions-security.md)、[MCP 与 Skills](docs/architecture-guide/09-mcp-and-skills.md)。
- Agent / Live / Provider 目录：[原生多 Agent](docs/architecture-guide/10-native-multi-agent-runtime.md)、[实时语音](docs/architecture-guide/11-realtime-voice-runtime.md)、[Provider 目录](docs/architecture-guide/15-provider-catalog.md)。

## 实现约束

### 权威、Interface 与兼容

- 每个状态只有一个运行时权威；兼容 Adapter 只投影到该权威，并说明删除条件。Skill 不拥有运行时状态，SessionRepository 不执行调度。
- 扩展已有深 Module；有第二个真实 Implementation 或明确替换需求才增加公开抽象。删除后复杂度不会散落到调用者的转发层应消除。不按行数拆 Module，不为假设需求加字段/Hook/策略。
- 使用 Module、Interface、Implementation、Seam、Adapter、Depth、Leverage、Locality 描述设计。注入外部依赖；异步路径不阻塞 I/O，复用执行/Sandbox Seam，取消/超时清理 stream、task、进程树。
- Python 使用 `from __future__ import annotations`、严格 Pyright 和 Ruff（行宽 110）。外部/持久化输入使用严格 Pydantic 契约，通常 `extra="forbid"`；领域状态优先 `StrEnum`。
- 删除需有零生产引用、权威替代、不可达或契约删除证据。Textual/FastAPI/Typer/Pydantic/capability 的反射入口不能仅凭文本引用或 Vulture 删除。同步删除死实现专属测试，保留公开/兼容契约测试。
- 主 turn 不再运行 PydanticAI `Agent` graph；无工具的摘要/提取仍可使用它，不应一并删除。

### 持久化、恢复与完成

- Session 是 append-only journal。当前 schema v11，v1–v10 必须可加载且不改写；升级只追加 `schema_upgrade`。配置 schema v2，v1 只在内存迁移并警告，不自动重写用户配置。
- 大载荷进入内容寻址 ArtifactStore，journal 留引用和有界摘要。压缩保留 raw history，以绝对 `source_end` 去覆盖前缀；未持久化 checkpoint 不作持久父节点，瞬时 Skill/Memory/MCP 内容不重复入历史。
- 主模型重试只由 Loop 管理；默认无次数/请求总时限硬墙，显式预算有效。可撤回候选文字但保留已完成工具，不执行半截参数或透明重放未知外部/原生工具动作。故障分类、心跳与恢复矩阵按长任务恢复文档验证，覆盖自动重连和 Session 重载。
- 普通问答不创建 Work Product，正文按需读。mutation 记录 prepared/applied/verified/failed/rolled_back；selector 多候选拒绝，局部修改验证目标变化、非目标不变。
- 完成门禁阻止未验证 mutation、strict 模式未处理 unknown effect、未结束/未送达/失败未处理 Agent、待审批/导入、冲突及缺失 evidence。Coordinator 持久化后才发布 terminal，每个 run 最多一个。

### 工具与权限

- `Risk` 控制审批，`EffectKind` 控制副作用追踪与验证，`ToolConcurrency` 控制调用并发；三者分开声明。调用时仅显式 `PARALLEL_SAFE` 可并行，`EXCLUSIVE` 是 barrier。
- 本地/MCP 工具经 Gateway；canonical output 派生模型文字和客户端展示，post Hook 不改 canonical output/Effect。工具与 UI 不自行写 timeline。Provider 原生工具属于 ModelDriverRequest，不属于 `search_tools` 目录。
- MCP/插件未声明副作用默认 unknown。Lumen Plan 只信任 Contract 的 `Risk=read`，不猜命令 basename；Git 检查用 `git_status` / `git_diff`。
- Lumen Git stage/commit/push 由根 Host 的结构化 Interface 持有：stage 不执行仓库 content filter；commit/push 为 `Risk=confirm`，每次新审批，auto、记忆规则和 child 均不能放宽。
- 文件经 workspace 路径解析，拒绝 `..`、绝对路径/符号链接逃逸。Sandbox 与审批正交，`workspace_write` fail closed。`run_command` 只提供 execution receipt，不代表捕获全部文件副作用。
- command Hook 使用 SandboxRunner，启动或沙箱失败即拒绝；Python Hook/Plugin 是显式信任的 Host 进程内代码。secret、API key、个人数据和完整凭据不进入日志、Session、fixture、diff 或 Artifact 摘要。
- Lumen child 深度最多一，工具为父有效集合 ∩ 角色 allow-list ∩ workspace mode，无多 Agent 控制工具；角色/Skill 不扩大权限。explorer 共享只读，default/worker 写独立 worktree；校验父 Session 所有权，spawn 幂等。
- worktree 导入检查基线、父 dirty path 和三方冲突，并验证导入。`LegacyChildRunAdapter` 只投影到 AgentOrchestrator；Session fork 不复制运行中执行，未完成 Agent 记录 `not_carried`。

## 验证与生成物

根目录安装：`uv sync --frozen --all-groups`；Web：`pnpm --dir src/web install --frozen-lockfile`。CI 使用 Node 22/pnpm 10；升级依赖同步 lockfile。
增强提取/实时语音回归安装 `--extra web --extra live`，测试命令同样显式带上这两个 extra。
CI 的质量任务不安装可选 extra，按 Linux/Darwin/Windows 分别执行 Pyright；全量测试覆盖
Linux Python 3.11–3.13、macOS/Windows Python 3.13。安全文档读取和后台 Web 启动仅支持
macOS/Linux；Windows 不得使用普通路径检查后打开来绕过 descriptor/no-follow 边界。

先最小回归，再按风险扩展；通过后不无故重复。纯 Markdown 修改检查事实、链接、diff 及受影响生成物，无需全套测试/构建。

| 改动范围 | 验证要求 |
|---|---|
| Python | `uv run ruff check .`、`uv run pyright`、相关 pytest |
| Host / schema / API | 加跑 `uv run pytest tests/test_workspace_host.py tests/test_web_api.py`，检查受影响 Default、Plan、TUI、Web、headless 共用契约及 OpenAPI |
| 配置 / Session / Runtime / Loop / Context / TaskWorkspace / Agent 生命周期 | 最小回归后运行 `uv run pytest` 全量 |
| TUI | 相关交互测试和 `tests/test_tui_snapshots.py`；仅预期视觉变化更新 snapshot |
| Web | `pnpm --dir src/web test`、`pnpm --dir src/web typecheck`、`pnpm --dir src/web build` |
| Provider / SDK | 目录、reasoning、models、Driver/Loop 请求级测试；HTTP MockTransport 验证实际参数，见 Provider 目录文档 |
| 打包 / 入口 | `uv build`；在隔离环境安装生成 wheel，执行 `lumen --version` / `lumen --check-config`，发布验收执行两者 |

生成物只通过命令更新，不手工编辑；改到其事实来源时同步更新并检查：

| 生成物 | 更新命令 | 新鲜度检查 |
|---|---|---|
| `docs/generated/contracts.json` | `uv run python -m lumen.contracts --write` | `uv run python -m lumen.contracts --check` |
| `src/web/openapi.json`、`src/web/src/lib/api/schema.generated.ts` | `pnpm --dir src/web api:schema` | `pnpm --dir src/web api:schema:check` |
| `docs/generated/provider-reasoning.md` | `uv run python scripts/export_provider_catalog.py` | 同命令加 `--check` |
| `docs/architecture-guide/content.generated.js` | `uv run python scripts/build_architecture_atlas.py` | 同命令加 `--check` |

`api:schema:check` 会重新生成文件并与 Git 比较；已修改的 schema 会显示差异，需检查是否为预期。架构文档改动同时核对 Atlas `index.html` 手写图解，重建 content 不会修正它。

`src/web/out/` 不入 Git。发布先构建 Web，再 `uv build`，否则 wheel 可能不含静态资源；`lumen web` 优先用内置 `api/static/`，源码运行也需前端构建。任意目录安装用 `python3 /absolute/path/to/lumen/scripts/install_editable.py`；`uv tool install --editable .` 须在仓库根目录。

交付说明行为变化、依据和实际验证；删除时给证据及保留的兼容/安全/恢复路径。说明未运行项与阻塞；fixture/MockTransport/构建不代表真实 Provider 或性能验收。完整 CI 见 `.github/workflows/ci.yml`。
