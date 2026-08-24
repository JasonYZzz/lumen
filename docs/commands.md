# Lumen 命令说明

本文档对应当前 `lumen` CLI、TUI slash 命令注册表和 Web 命令实现。CLI 的实时帮助以 `lumen --help` 为准；TUI slash 命令的单一事实源是 `src/lumen/ui/slash_commands.py`。

## 1. CLI 入口

### 1.1 启动 TUI

```bash
lumen [OPTIONS]
lumen --cwd /path/to/project
lumen --model <configured-name>
lumen --resume <session-id>
```

| 选项 | 简写 | 说明 |
|---|---|---|
| `--config <path>` | `-c` | 独占使用一个 YAML 配置文件，不参与分层发现 |
| `--cwd <path>` | — | 暴露给文件工具、命令和 stdio MCP 的工作区，默认 `.` |
| `--resume <id>` | — | 恢复一个持久化 session |
| `--model <name>` | `-m` | 选择 `agent.models` 中配置的逻辑模型名 |
| `--check-config` | — | 校验配置、信任和工具发现后退出，不调用模型 |
| `--dump-effective-config` | — | 输出脱敏后的最终配置、来源和字段 provenance 后退出 |
| `--version` | `-V` | 输出版本号后退出 |
| `--install-completion` | — | 为当前 shell 安装补全 |
| `--show-completion` | — | 输出当前 shell 的补全脚本 |

### 1.2 Headless 单次执行

```bash
lumen -p "解释这个项目"
lumen -p "运行检查" --permission-mode accept_edits
lumen -p "生成摘要" --output-format json
lumen -p "继续" --resume <session-id>
```

`--print/-p` 运行一次非交互 Agent turn，照常持久化 session。`--output-format` 支持 `text` 和 `json`；`--permission-mode` 支持 `manual`、`accept_edits`、`auto`。Plan 是独立的 session collaboration mode，可由 `collaboration.default_mode: plan` 配置；headless 无法弹出审批 UI，因此 `manual` 下需要人工确认的调用会被自动拒绝。

### 1.3 Web 客户端

```bash
lumen web --cwd .
lumen web --cwd . --model <name> --resume <session-id>
lumen web --background
lumen web --status
lumen web --stop
```

| 选项 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址与端口；当前只允许 loopback，默认 `127.0.0.1:8765` |
| `--no-open` | 不自动打开浏览器 |
| `--api-only` | 仅启动 FastAPI，不提供静态页面 |
| `--background/-d` | macOS/Linux 后台运行，状态与日志写入 `.lumen/` |
| `--status` / `--stop` | 查询或停止当前工作区的后台 Web 进程 |
| `--access-log` | 输出逐请求访问日志 |

Web 右上角设置入口始终可见。模型配置写入 `<workspace>/.lumen/agent.web.yaml` 受管覆盖层，
不会重写已有 User/Project/Local 文件；只接受密钥环境变量名，保存后需重启 Web 才会生效。
活动 Run、显式 `--config` 模式、并发版本冲突或未受管的同名文件会阻止写入。
从旧的单模型形式首次添加模型时，受管层会保留原模型和原默认选择；若原模型使用内联密钥，
必须先把它改为环境变量引用，避免凭据进入 Web 管理的复制路径。

### 1.4 Capability inventory

```bash
lumen capabilities --cwd . --json
```

该命令通过与 TUI/Web 相同的 `ResourceManager.capabilities_report()` 只读 Interface，列出 Tool、Skill、MCP server 和 Agent Profile。Tool 行包含 origin、loaded/deferred/disabled 状态、Risk、EffectKind、审批决定、Sandbox mode 与输入 schema digest；报告不参与权限决策。

### 1.4 配置、信任与 MCP 审批

```bash
lumen init [--cwd PATH | --global | --local]
lumen trust --cwd PATH
lumen trust --revoke --cwd PATH

lumen mcp list --cwd . [--config PATH]
lumen mcp approve <name> --cwd . [--config PATH]
lumen mcp deny <name> --cwd . [--config PATH]
lumen mcp reset [--name <name>] --cwd . [--config PATH]
```

`mcp reset` 使用 `--name`；省略时清除当前项目的全部持久化 MCP 决定。它不会删除 MCP 配置本身。

## 2. TUI slash 命令

在 TUI 输入 `/` 可打开补全，`/help` 按分组输出注册命令；下表为便于查阅，将 `/resource` 的三个子形式拆成独立行。`/quit` 仍可调用，但只是 `/exit` 的隐藏兼容别名，不出现在帮助、补全或命令面板中。

| 分组 | 命令 | 作用 | 运行期间 |
|---|---|---|---|
| Session | `/new` | 新建 session | 禁用 |
| Session | `/sessions` | 列出历史 session | 可用 |
| Session | `/agents [interrupt\|message\|continue\|import\|reject\|close] <id> [text]` | 查看、协调、中断、导入/拒绝或关闭 Agent；`/children` 为可见兼容入口 | 可用 |
| Session | `/checkpoints` | 浏览 receipts，并从历史 turn 创建非破坏式 session 分支 | 禁用 |
| Session | `/resume <id>` | 恢复指定 session | 禁用 |
| Session | `/clear` | 清空可见时间线，保留 session 上下文 | 禁用 |
| Session | `/retry` | 重发上一条 prompt | 需先结束当前 run |
| Session | `/edit` | 用 $VISUAL/$EDITOR 编辑上一条 prompt，在保留之前 turn 的新分支上重发；工作区文件不变 | 禁用 |
| Model | `/model [name]` | 无参数列模型；有参数切换模型 | 查看可用；切换会被后端保护 |
| Model | `/mode [manual\|accept_edits\|plan\|auto]` | 查看或切换审批模式 | 可用 |
| Model | `/status` | 查看 workspace、session、模式、沙箱和 UI 状态 | 可用 |
| Context | `/context [--json\|sources\|capabilities]` | 查看预算、活动来源或统一能力清单 | 可用 |
| Context | `/compact [focus]` | 请求强制压缩，可附 focus | 可用 |
| Context | `/clarification cancel` | 取消待回答澄清 | 可用 |
| MCP | `/mcp` | 查看 MCP 连接、工具数和 deferred 状态 | 可用 |
| MCP | `/resources` | 列出可激活的 MCP resources | 可用 |
| MCP | `/resource <server::uri>` | 激活 resource 到当前 session | 可用 |
| MCP | `/resource refresh <ref>` | 刷新当前 session 的 resource snapshot | 可用 |
| MCP | `/resource unload <ref>` | 卸载当前 session 的 resource | 可用 |
| MCP | `/prompts` | 列出 MCP prompt 模板 | 可用 |
| MCP | `/prompt <server:name> [key=value ...]` | 渲染模板并作为一次 Agent run 提交 | 需先结束当前 run |
| Memory | `/memory [action]` | 管理持久记忆，详见下节 | 可用 |
| Other | `/theme [lumen-dark\|lumen-light]` | 查看或即时切换主题 | 可用 |
| Other | `/transcript` | 打开可搜索、可展开/Raw/复制的结构化 transcript | 可用 |
| Other | `/help` | 显示分组命令帮助 | 可用 |
| Other | `/tools` | 列出模型可见工具 | 可用 |
| Other | `/hooks` | 列出 hooks、最近触发与 deny 统计 | 可用 |
| Other | `/copy` | 复制最近一条完整助手回复 | 可用 |
| Other | `/skills` | 列出发现的 Skills | 可用 |
| Other | `/skill:<name> [args]` | 激活并手动运行 Skill | 需先结束当前 run |
| Other | `/skill unload <name>` | 从当前 session 卸载 Skill | 可用 |
| Other | `/exit` | 退出 TUI；若正在运行则先取消 | 取消后退出 |

说明：`/resource` 的完整 reference 形式为 `<server>::<uri>`；当 resource 名称或 URI 唯一时，也可以使用短引用。`/prompt` 的完整 reference 形式为 `<server>:<name>`。

### 2.1 Memory actions

```text
/memory list
/memory remember <text> [--scope project|user]
/memory forget <id|text>
/memory edit <id>
/memory edit <id> --apply
/memory edit <id> --set <new content>
/memory use on|off
/memory learn on|off
/memory incognito on|off
/memory rebuild
```

无 action 时等同于 `list`。`edit <id>` 导出私有 Markdown 草稿；`--apply` 校验并写回 SQLite 权威存储，`--set` 直接替换内容。

## 3. Web slash 命令支持矩阵

Web 通过 FastAPI application host 执行命令；本地显示类命令不会伪造 Agent run。`/context sources`、`/context capabilities`、`/prompts`、`/hooks` 使用只读 Interface，Web 的同一能力投影位于 `GET /api/v1/capabilities`；`/prompt` 在后端渲染模板后启动 run，`/copy` 使用浏览器剪贴板。

| 能力 | Web 命令 | 备注 |
|---|---|---|
| 会话 | `/new`、`/retry`、`/clear` | 历史列表、恢复、重命名、归档与删除由侧边栏提供；空 Session 不进入历史列表 |
| 模型/审批 | `/model [name]`、`/mode [mode]` | `auto` 仍需显式确认 |
| Context | `/context`、`/context sources`、`/context capabilities`、`/compact [focus]`、`/clarification cancel` | sources/capabilities 不依赖 context engine 报告可用性 |
| Memory | `/memory [action]` | 支持常用 list/remember/forget/use/learn/incognito 操作 |
| MCP | `/mcp`、`/resource [refresh\|unload] <ref>`、`/prompts`、`/prompt <ref> [key=value ...]` | `/prompt` 支持单/双引号参数值 |
| Skill | `/skills`、`/skill:<name> [args]`、`/skill unload <name>` | 动态 Skill 也进入补全 |
| 其他 | `/tools`、`/hooks`、`/copy`、`/help` | `/copy` 复制最新助手回复 |

Web 当前不提供 `/sessions`、`/resume`、`/resources`、`/theme`、`/exit` 的 slash 形式；其中 session 浏览/恢复由侧边栏承担。TUI 与 Web 同一工作区同时只允许一个 Agent run，运行期间的状态变更命令可能返回 workspace busy。

## 4. 快捷键与发现入口

- TUI：`Ctrl+P` 打开命令面板，`Alt+C` 等同 `/copy`，`Shift+Tab` 循环审批模式。
- TUI/Web：输入 `/` 打开 slash 菜单；输入 `@` 打开文件 mention 补全。
- TUI：`Esc` 依次处理关闭补全、取消 run、清空输入；空闲退出使用 `/exit` 或 `Ctrl+C`。
- Web：会话切换、恢复和新建也可直接使用左侧栏。

## 5. 维护约定

- TUI 命令必须先更新 `src/lumen/ui/slash_commands.py`，不要在 help、补全或 command gate 中另建手写清单。
- Web 新命令必须同步更新 `src/web/src/lib/slash-commands.ts`、前端 handler、API client/schema 与 `/help` 文本。
- CLI 选项以 Typer 定义和 `lumen --help` 为准；修改后同步更新本文件与 README。
