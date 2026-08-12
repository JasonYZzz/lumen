# Lumen 架构与核心实现指南

> **网页版入口：** 直接打开 [`index.html`](index.html)。它是无外部依赖的离线液态玻璃架构手册，包含可搜索导航、响应式布局、打印样式和精确技术图。以下 Markdown 文件保留为文字源和轻量阅读版本。

这套文档面向希望真正理解 Lumen 实现逻辑的维护者。它不按文件列表复述代码，而是围绕系统中的深 module、interface、seam、状态变化和失败路径展开。

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

## 一句话架构

Lumen 是一个本地优先、事件驱动的 coding-agent 框架：`WorkspaceHost` 统一承接客户端命令，`RunCoordinator` 管理可恢复会话，`AgentRuntime` 执行模型—工具循环，`ContextEngine` 在每次模型调用前后管理有界上下文，所有公开状态通过类型化 `RunEvent` 投影给 TUI、Web 和 JSONL 会话仓库。

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

- 单个 workspace 同时只允许一个主 Agent run。
- 本地 `run_command` 有工作区约束、超时和有界输出，但不是 OS 级沙箱。
- 子 Agent 当前是 Session 内持久化的单层 Agent Thread；读任务共享只读工作区，写任务使用隔离 worktree，并由根 Agent 负责协调和综合。
- TUI 与 Web 共用运行逻辑，但 UI 渲染分别由 Textual 和 Next.js 实现。
- Realtime 语音是可选 Web transport；它共用 Host 能力和 Session，但原始音频默认不持久化，进程重启后也不会自动重放未确认的远端动作。
- MCP 工具可以延迟暴露 schema；MCP resource 和 prompt 只有显式激活后才进入上下文。
