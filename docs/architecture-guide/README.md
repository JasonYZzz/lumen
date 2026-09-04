# Lumen 架构与核心实现指南

> **网页版入口：** 直接打开 [`index.html`](index.html)。它是无外部运行时依赖的离线 Architecture Atlas，包含可搜索导航、响应式布局、打印样式、精确技术图，以及在当前页面打开 Markdown 与源码的 Trace Reader。以下 Markdown 文件仍是可审计的文字事实来源。

Atlas 不维护第二套技术正文。`content.generated.js` 是当前架构/命令/扩展文档、核心 Python/Web 源码、
契约测试和生成契约的确定性只读快照；无论通过 HTTP 还是直接打开 `index.html`，Trace Reader 都读取
这份经过 freshness 校验的离线证据。`docs/research/`、`docs/plans/` 和隔离 spike 是
时间点证据，不进入“当前实现”全文检索。完整文档分层见 [`docs/README.md`](../README.md)。修改相关
正文或源码后运行：

```bash
uv run python scripts/build_architecture_atlas.py
uv run python scripts/build_architecture_atlas.py --check
```

CI 会执行 freshness 检查，防止新版实现已经落地但网页版仍携带旧内容。

这套文档面向希望真正理解 Lumen 实现逻辑的维护者。它不按文件列表复述代码，而是围绕系统中的深
Module、Interface、Seam、状态变化和失败路径展开。

## 建议阅读顺序

1. [系统全景与设计原则](01-system-overview.md)：Lumen 是什么，主要 module 如何协作。
2. [完整 Agent 运行流程](02-agent-runtime-flow.md)：一条 prompt 从提交到最终回答的完整时序。
3. [上下文、压缩与记忆](03-context-and-memory.md)：上下文预算、checkpoint、artifact 与长期记忆。
4. [工具、权限与安全](04-tools-permissions-security.md)：工具发现、风险分类、审批、hook 与执行约束。
5. [会话、事件与客户端](05-sessions-events-clients.md)：TUI/Web 如何共享同一运行内核。
6. [扩展体系与子 Agent](06-extensions-and-delegation.md)：MCP、Skill、插件、Hook 和原生 Agent Thread。
7. [MCP 与 Agent Skills 深入说明](09-mcp-and-skills.md)：两类扩展的接入、上下文、权限与安全边界。
8. [配置、数据与生命周期](07-configuration-and-data.md)：配置合并、持久化格式和资源启动顺序。
9. [源码导航与调试指南](08-code-navigation.md)：修改功能时应该从哪里进入、如何验证。
10. [原生多 Agent Runtime 决策记录](10-native-multi-agent-runtime.md)：控制面、Runtime Factory、安全恢复与兼容性不变量。
11. [Web Realtime 语音 Runtime 决策记录](11-realtime-voice-runtime.md)：WebRTC、sideband、能力网关、恢复与完成门禁。
12. [当前实现审计](12-current-implementation-audit.md)：按源码、schema 与契约测试核对状态权威、兼容版本和文档漂移。
13. [LumenAgentLoop 单轨决策记录](13-native-agent-loop-migration.md)：自研 Loop 的权威、Driver/能力/恢复契约与旧 Agent graph 删除证据。
14. [长任务持续执行、恢复与排障](14-long-running-recovery.md)：滑动空闲、重试、同轮压缩、失败续跑、计数语义和踩坑检查。

## 一句话架构

Lumen 是一个本地优先、事件驱动的 coding-agent 框架：`WorkspaceHost` 统一承接客户端命令，`RunCoordinator` 管理可恢复会话，`AgentRuntime` 执行模型—工具循环，`ContextEngine` 在每次模型调用前后管理有界上下文，所有公开状态通过类型化 `RunEvent` 投影给 TUI、Web 和 JSONL 会话仓库。

当前实现还用 Tool Contract V2 统一 canonical 输出、模型文本与客户端展示，用 `RegistrationScope` 管理可逆注册，并将 provider 请求回执、能力观测和 generated contract catalog 作为可审计但非权威的只读投影。

`LumenAgentLoop` 是单 Agent 模型—工具循环的唯一权威；`PydanticAIModelDriver` 只在
`ModelDriver` seam 下提供成熟的 Model/Provider wire translation 与资源生命周期。`AgentRuntime`、
`ContextEngine`、`CapabilityGateway`、Session v9 与完成门禁各自保持单一权威，不存在运行时 selector
或隐藏旧 Agent graph 回退路径。Context 摘要与 Memory 提取中的无工具 typed Agent 仅是结构化输出
Adapter，不是第二套模型—工具 Loop。

## 关键术语

| 术语 | 在 Lumen 中的含义 |
|---|---|
| module | 拥有清晰 interface 和内部实现的功能单元，例如 `ContextEngine`。 |
| interface | 调用者必须了解的全部约定，包括输入、输出、不变量、错误与性能特性。 |
| seam | 可以替换 adapter 而无需修改调用方的位置，例如 `WorkspaceResources`。 |
| adapter | 在 seam 上连接具体技术的实现，例如 TUI、FastAPI、MCP toolset。 |
| active history | 下一次模型调用实际携带的有界历史，不等于完整持久化历史。 |
| raw history | JSONL 中追加保存的完整会话事实，用于审计与恢复。 |

## 当前实现边界

- 单个 workspace 同时只允许一个执行 writer：进程内 active run 由 `WorkspaceHost` 管理，跨进程竞争由
  workspace 级 OS advisory execution lock 阻止；只读 Session/Timeline 操作不受影响。
- 本地 `run_command` 同时经过审批与 OS sandbox：默认 `workspace_write`，macOS 使用 Seatbelt、Linux 使用 bubblewrap；adapter 缺失时 fail closed。只有显式 `sandbox.mode: disabled` 才放弃隔离。
- 子 Agent 当前是 Session 内持久化的单层 Agent Thread；读任务共享只读工作区，写任务使用隔离 worktree，并由根 Agent 负责协调和综合。
- TUI 与 Web 共用运行逻辑，但 UI 渲染分别由 Textual 和 Next.js 实现。
- Realtime 语音是可选 Web transport；它共用 Host 能力和 Session，但原始音频默认不持久化，进程重启后也不会自动重放未确认的远端动作。
- MCP 工具可以延迟暴露 schema；MCP resource 和 prompt 只有显式激活后才进入上下文。
- 当前 Session schema 是 v9；v1–v8 只读加载，首次写入较新 record 时追加 `schema_upgrade`，不会重写 header 或历史行。
- `Risk`、`EffectKind`、`ToolConcurrency` 分别负责审批、效果追踪/验证与调用重叠；三者不能互相推断。
- 所有文件 mutation 经过 expected revision 与无符号链接 hop 的 `Workspace.atomic_write/remove`；冲突返回 `STALE_RESOURCE`，不会猜测覆盖。
- `lumen capabilities --json`、TUI `/context capabilities` 与 Web `/api/v1/capabilities` 使用同一只读能力投影。
