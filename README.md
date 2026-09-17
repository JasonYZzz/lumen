# Lumen

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/lumen-lockup-dark.svg">
    <img src="docs/brand/lumen-lockup-light.svg" width="620" alt="Lumen — open agent harness">
  </picture>
</p>

**可配置的 Python Agent 框架，让模型在工作区中调用工具、执行任务并保留可恢复的会话。**

Lumen 提供终端 TUI、本机 Web 和脚本 CLI 三种入口，共用会话、工具、审批与上下文管理。
模型负责推理，Lumen 自己管理模型与工具的执行循环；可接入 OpenAI、Anthropic、Google、
Ollama 及 OpenAI-compatible 服务，并通过 MCP、Skills 和 Python 插件扩展能力。

## 核心能力

- **工作区任务**：读取与搜索文件，按配置启用编辑、命令执行、结构化 Git 操作和网页检索。
- **模型切换**：配置多个模型与服务端点，按模型能力选择推理强度、输入类型和原生联网工具。
- **能力扩展**：连接 stdio / Streamable HTTP MCP 服务；按需加载 Skills，接入工具插件与生命周期 Hooks。
- **多 Agent 协作**：并行处理独立任务；只读探索共享工作区，写入任务使用隔离 Git worktree，检查后导入结果。
- **持续执行与恢复**：流式进度、计划与证据验证、上下文自动压缩、会话恢复，以及可管理的持久记忆。
- **交互界面**：TUI 与 Web 提供工具过程、审批和会话管理；Web 还可选启用实时语音。

## 快速开始

要求 **Python 3.11–3.13**。以下命令在仓库根目录执行，使用已安装的 `uv`。
macOS/Linux 提供完整的本地沙箱与安全文档预览；Windows 不支持后台 Web 启动和安全文档
预览/下载，默认沙箱不可用时仍拒绝执行命令，不会自动降为无沙箱模式。

### 1. 安装并初始化

```bash
uv sync --frozen --all-groups
uv run lumen init --global
```

初始化会创建 `~/.lumen/agent.yaml`，不会覆盖已有文件。模板默认使用 `openai:gpt-5`，
通过 `OPENAI_API_KEY` 读取密钥，仅启用文件读取、目录浏览和文本搜索工具。
编辑该文件，填入你有权访问的模型与密钥环境变量名，再在启动 Lumen 的终端中设置对应环境变量。
其他模型、写入工具和 MCP 配置可参考 [agent.example.yaml](agent.example.yaml)。

### 2. 检查配置并启动

```bash
uv run lumen --check-config
uv run lumen
```

配置检查会校验配置并发现已批准的工具，可能连接 MCP 服务；**不会调用模型，也不证明模型 API 可用**。
项目包含配置或 Skills 时，首次交互启动会请求信任。非交互环境需先审查项目，再执行
`uv run lumen trust --cwd .`。

TUI 中 `Enter` 发送，`Shift+Enter` 换行；输入 `/help` 查看命令，`/model` 切换模型，
`/thinking` 选择可用推理档位，`/sessions` 查看历史会话。

### 3. 选择其他入口

**单次执行**，适合脚本与 CI：

```bash
uv run lumen -p "概述这个项目的结构"
uv run lumen -p "概述这个项目的结构" --output-format json
```

Headless 模式也会保存会话；默认 `manual` 审批模式下，需要人工确认的工具调用会被拒绝。

**Web 界面**，从源码运行时先构建前端（Node.js 22、pnpm 10）：

```bash
pnpm --dir src/web install --frozen-lockfile
pnpm --dir src/web build
uv run lumen web --cwd .
```

默认在 `127.0.0.1:8765` 启动并打开浏览器，当前仅支持本机访问。
已包含静态前端的安装包运行时不需要 Node.js。
实时语音需另装 `live` extra 并配置路由，见[实时语音指南](docs/architecture-guide/11-realtime-voice-runtime.md)。
网页正文增强提取可安装 `web` extra；JavaScript 页面渲染可选安装 `browser` extra 并执行
`crawl4ai-setup` 下载浏览器。未安装增强依赖时保留标准库文本提取，真实浏览器验收需单独运行。

**在其他项目使用**，先从仓库根目录安装全局命令：

```bash
uv tool install --editable .
lumen --cwd /path/to/project
```

`--cwd` 指定工具实际操作的工作区，默认是当前目录；配置文件的位置不会改变它。
完整选项、后台 Web 管理和会话恢复命令见[命令说明](docs/commands.md)。

## 配置与安全

### 按项目组合配置

```bash
lumen init          # 项目共享配置：.lumen/agent.yaml
lumen init --local  # 本机覆盖配置：.lumen/agent.local.yaml
```

常用配置按用户 → 项目 → 本机 → Web 受管层覆盖。`--config /path/to/agent.yaml`
可独占加载一个文件；旧版根目录 `agent.yaml` 仍兼容。
密钥使用 `api_key_env` 引用环境变量；只加载可信配置，因为配置可以启动 MCP 进程或加载 Python 插件。
完整合并规则见[配置指南](docs/architecture-guide/07-configuration-and-data.md)。

### 执行边界

- **能力按配置启用**：需要修改文件或执行命令时，在 `tools.builtins` 中添加相应工具；审批与沙箱仍然有效。
- **Plan 与审批独立**：Plan 模式仅允许契约声明为只读的工具。Git commit / push 每次都需要显式审批。
- **本地进程受沙箱约束**：默认 `workspace_write`，macOS 使用 Seatbelt、Linux 使用 bubblewrap，命令网络默认关闭；沙箱不可用时拒绝执行。
- **外部能力有独立边界**：MCP、Host 网页请求与 Provider 原生工具不由本地进程沙箱统一隔离；Python 插件是受信任的进程内代码。
- **会话与记忆可追溯**：会话追加保存在工作区 `.lumen/sessions/`；自动记忆学习默认关闭。执行结果不确定的外部操作不会透明重放。

工具权限、外部副作用与恢复规则见[工具与安全](docs/architecture-guide/04-tools-permissions-security.md)。

## 深入使用

| 需求 | 文档 |
| --- | --- |
| 查 CLI、TUI、Web 命令 | [命令说明](docs/commands.md) |
| 配置模型、工具与 MCP | [配置示例](agent.example.yaml) · [配置指南](docs/architecture-guide/07-configuration-and-data.md) |
| 查模型推理与原生联网能力 | [Provider 能力目录](docs/generated/provider-reasoning.md) |
| 接入 MCP、安装或编写 Skill | [MCP 与 Skills](docs/architecture-guide/09-mcp-and-skills.md) · [Skill 作者指南](docs/skills/authoring.md) |
| 接入 Hooks | [Hook 指南](docs/hooks/README.md) |
| 使用多 Agent 协作 | [原生多 Agent](docs/architecture-guide/10-native-multi-agent-runtime.md) |
| 理解压缩、记忆、重试与恢复 | [上下文与记忆](docs/architecture-guide/03-context-and-memory.md) · [长任务排障](docs/architecture-guide/14-long-running-recovery.md) |
| 阅读架构与源码 | [架构指南](docs/architecture-guide/README.md) · [源码导航](docs/architecture-guide/08-code-navigation.md) · [交互式 Atlas](docs/architecture-guide/index.html) |

全部文档及当前实现与研究资料的分层说明见[文档地图](docs/README.md)。

## 开发与验证

Python 包与 CLI 名称为 `lumen`，发行包名称为 `lumen-agent`。
核心代码在 `src/lumen/`，Web 在 `src/web/`，测试在 `tests/`。

```bash
uv run ruff check .
uv run pyright
uv run pytest

# 启用网页正文增强提取与实时语音的回归测试
uv sync --frozen --all-groups --extra web --extra live
uv run --frozen --extra web --extra live pytest

# 修改 Web 时
pnpm --dir src/web test
pnpm --dir src/web typecheck
pnpm --dir src/web build
```

打包执行 `uv build`；包含 Web 的发行包需先构建前端。
生成物更新、契约检查及具体改动的验证要求见 [AGENTS.md](AGENTS.md)。

[GitHub Actions CI](.github/workflows/ci.yml) 是版本控制中的质量门禁，不是发布脚本：主分支
推送与 PR 自动触发，也支持手动运行；同一分支的新运行取消旧运行。静态检查与生成物检查
集中执行，类型检查覆盖 Linux/macOS/Windows；全量测试覆盖 Linux 的 Python 3.11–3.13 和
macOS/Windows 的 Python 3.13。另验证未安装可选网页依赖时的降级路径，以及 Web
契约、测试、构建与打包。检查失败不代表推送失败，也不应通过删除 CI 隐藏问题。
