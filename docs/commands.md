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
| `--thinking <level>` | — | 为当前 Session 选择推理强度；适用于 TUI 与 headless |
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
lumen web --cwd . --thinking medium
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
| `--status` / `--stop` | macOS/Linux 查询或停止当前工作区的后台 Web 进程 |
| `--access-log` | 输出逐请求访问日志 |

Web 右上角设置入口始终可见。模型配置写入 `<workspace>/.lumen/agent.web.yaml` 受管覆盖层，
不会重写已有 User/Project/Local 文件；只接受密钥环境变量名。新增模型、修改当前模型和设为默认会
同步更新运行时模型 registry，需要重建时采用 candidate Runtime 原子切换，无需重启 Web。
活动 Run、显式 `--config` 模式、并发版本冲突或未受管的同名文件会阻止写入。

TUI 与 Web 的 `/thinking` 打开推理档位选择；`/thinking low` 直接选择。
Web 输入框旁也提供当前档位入口，保留未发送草稿。可用档位来自所选模型能力，
`provider_default` 与 `off` 含义不同；不支持关闭的模型会拒绝 off。
运行中可查看，不能修改；选择通过 Host 按 Session、模型持久化，下次 Run 生效。
CLI `--thinking` 可覆盖恢复 Session 的旧选择；省略则恢复原选择或模型配置。
启动覆盖只属于启动模型；显式切换模型后清除该启动覆盖，已保存的各模型 Session 选择保留。
未知部署可在模型配置声明 `reasoning_levels: [low, medium, high]`，声明前须确认部署支持。
旧 settings 推理配置标记为未校验；不能把 Provider 默认显示为已知实际强度。
官方 DeepSeek Flash、Kimi Coding K3 和百炼 Anthropic Qwen 3.8 已内置能力映射，无需手工声明档位。
选择器优先显示模型的实际档位，当前保存的兼容别名仍用 `medium → high` 等标签显示。
`provider_default` 保持不指定强度；Web 显示“跟随供应商默认（high）”等有官方依据的缺省值，
工具栏简写为“默认 · high”。这是文档默认值，不是请求中显式传入或实际观测到的强度。
同系列模型可以拥有相同档位；跨模型切换会刷新菜单，新任务不会继承上个任务的推理选择。
DeepSeek Flash 实际强度为 low/high/max，Qwen 3.8 为 low/medium/xhigh；K3 不提供 off，
避免关闭思考时被 Coding 路由切换到另一个模型。未知或不支持调节时禁用单一默认选项并说明原因。
Web 设置页根据当前编辑的模型 ID、协议和地址查询 Host 能力，避免固定显示所有通用档位。
能力来源采用[Provider 目录](generated/provider-reasoning.md)。自定义代理可在模型配置或 Web 设置
使用 `reasoning_profile`（Web 字段 `reasoningProfile`）引用经过核对的规则；模型 ID 和协议须一致。
SDK 的名称推断不再决定档位；更新目录的流程见[维护约定](architecture-guide/15-provider-catalog.md)。
从旧的单模型形式首次添加模型时，受管层会保留原模型和原默认选择；若原模型使用内联密钥，
必须先把它改为环境变量引用，避免凭据进入 Web 管理的复制路径。

设置窗口按通用设置、模型、扩展能力和 Agent 预设分类。模型通过表单顶部的“当前配置”选择，
右侧逐行编辑；保存操作固定在内容区底部。手机使用顶部分类和单列表单。切换分类保留未保存内容，
关闭时会将“继续编辑 / 放弃更改”提示带入视野，重复按 Esc 不会直接丢弃草稿。
有未保存内容时需先保存或还原，才能添加或选择其他模型；工作区任一任务运行时模型编辑为只读。
配置路径复制、版本冲突检查和模型配置即时生效的规则保持一致。

### 1.4 Capability inventory

```bash
lumen capabilities --cwd . --json
```

该命令通过与 TUI/Web 相同的 `ResourceManager.capabilities_report()` 只读 Interface，列出 Tool、Skill、MCP server 和 Agent Profile。Tool 行包含 origin、loaded/deferred/disabled 状态、Risk、EffectKind、审批决定、Sandbox mode 与输入 schema digest；报告不参与权限决策。

deferred 表示工具需要通过模型的 `search_tools` 发现后加载，不等于不可用。搜索支持关键词和
`queries: [""]` 分页浏览；Server 启动状态单独报告。Sandbox mode 描述命令子进程策略，不能据此
推断 MCP/Web 联网是否可用。Risk 与 EffectKind 分别控制审批与副作用追踪，read 风险不自动获得
observe 的断线重试语义。

### 1.5 配置、信任与 MCP 审批

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
| Session | `/children [cancel\|interrupt\|message\|continue\|import\|reject\|close] <id> [text]` | 查看、协调、中断、导入/拒绝或关闭 Agent；`/agents` 是可调用但暂不出现在 TUI 补全中的 canonical alias | 可用 |
| Session | `/checkpoints` | 浏览 receipts，并从历史 turn 创建非破坏式 session 分支 | 禁用 |
| Session | `/resume <id>` | 恢复指定 session | 禁用 |
| Session | `/clear` | 清空可见时间线，保留 session 上下文 | 禁用 |
| Session | `/retry` | 重发上一条 prompt，恢复附件/receipts 及已持久化完整工具批次；未解决 Effect 阻止执行 | 需先结束当前 run |
| Session | `/edit` | 用 $VISUAL/$EDITOR 编辑上一条 prompt，在保留之前 turn 的新分支上重发；工作区文件不变 | 禁用 |
| Model | `/model [name]` | 无参数打开可搜索选择器；有参数切换模型 | 运行中选择器只读；切换受 Host 保护 |
| Model | `/mode [manual\|accept_edits\|plan\|auto]` | 无参数打开模式选择器；有参数沿用模式切换契约 | 可用 |
| Session | `/tasks` | 展开或收起最近的计划步骤 | 可用 |
| Model | `/status` | 查看 workspace、session、模式、沙箱和 UI 状态 | 可用 |
| Context | `/context [--json\|sources\|capabilities]` | 查看预算、活动来源或统一能力清单 | 可用 |
| Context | `/instructions [--json]` | 查看 prompt mode、版本、稳定/动态大小与来源摘要 | 可用 |
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

Web 新任务页的九尾狐使用同一 WebGL canvas 合成透明视频与轻微琥珀色轮廓光；输入聚焦时减弱光线，角色回应时短暂增强。先加载 idle 视频，首帧显示后在浏览器空闲时预取回应视频；聚焦或点击会提前加载回应，动作仍在原动画的自然端点衔接。离屏或暂停时停止绘制并取消等待中的预取，reduced/static 模式不加载视频，WebGL 或解码失败回退到静态图片。桌面渲染 DPR 上限为 1.5，窄屏为 1；优先随视频解码帧绘制，不支持此接口时将绘制限制在 30 FPS。

Web 通过 FastAPI application host 执行命令；本地显示类命令不会伪造 Agent run。`/context sources`、`/context capabilities`、`/instructions`、`/prompts`、`/hooks` 使用只读 Interface；prompt 诊断投影位于 `GET /api/v1/instructions`，能力投影位于 `GET /api/v1/capabilities`；`/prompt` 在后端渲染模板后启动 run，`/copy` 使用浏览器剪贴板。

| 能力 | Web 命令 | 备注 |
|---|---|---|
| 会话 | `/new`、`/retry`、`/clear`、`/checkpoints`、`/dequeue` | 历史列表、恢复、重命名、归档与删除由侧边栏提供；checkpoint 可创建分支，dequeue 撤回尚未执行的排队输入 |
| Agent/审计 | `/agents`、`/transcript` | 打开 Agent 协调面板或结构化 transcript；具体 Agent 动作由面板/API 执行 |
| 模型/审批 | `/model [name]`、`/mode [mode]`、`/plan <task>` | `auto` 仍需显式确认；`/plan` 以 Plan collaboration mode 启动任务 |
| 计划 | `/tasks` | 打开最新计划侧栏；完成与跳过分开计数；此入口不批准执行 |
| Context | `/context`、`/context sources`、`/context capabilities`、`/instructions`、`/compact [focus]`、`/clarification cancel` | `/instructions` 不回显 prompt 正文 |
| Memory | `/memory [action]` | 支持常用 list/remember/forget/use/learn/incognito 操作 |
| MCP | `/mcp`、`/resource [refresh\|unload] <ref>`、`/prompts`、`/prompt <ref> [key=value ...]` | `/prompt` 支持单/双引号参数值 |
| Skill | `/skills`、`/skill:<name> [args]`、`/skill unload <name>` | 动态 Skill 也进入补全 |
| 其他 | `/tools`、`/hooks`、`/copy`、`/help` | `/copy` 复制最新助手回复 |

Web 当前不提供 `/sessions`、`/resume`、`/resources`、`/theme`、`/exit` 的 slash 形式；其中 session 浏览/恢复由侧边栏承担。TUI 与 Web 同一工作区同时只允许一个 Agent run，运行期间的状态变更命令可能返回 workspace busy。

Web 的 `/model`、`/mode` 无参数形式直接打开选择器，点击菜单项与完整 slash 命令共用处理路径。模型名来自配置注册表，未知名称不会被替换为列表第一项。输入区 `+` 提供常用操作，完整命令通过 `/` 检索；模型和审批的参数选项在检索时出现，避免占满主菜单。`@文件`、图片粘贴、拖放和附件能力检查保持有效。

Skill 补全以发现的 `name` 为标识，`description` 仅作辅助说明。Web 用名称和单行省略的说明组成紧凑菜单项，悬停可查看完整说明；TUI 保持 `/skill:<name>` 为主标签，说明中的换行只在显示时合并，并按终端宽度省略。两端选择 Skill 后均插入 `/skill:<name> `，不会把描述插入输入框，也不会在选中时立即发起 Skill 执行。

Web 的审批选择器和 Plan 状态标记显示当前 Session 的设置；刷新工作区、切换模型不会用工作区默认值覆盖它们。输入区不再提供工作方式下拉框；默认直接执行，单独输入 `/plan` 切换 Plan / Default，`/plan <task>` 直接发起规划。
新任务中的 `/mode auto` 和菜单选择使用同一确认流程：确认成功后更新显示；失败保留原模式并允许重试。
切换或离开任务时取消未提交的确认；运行期间禁止切换审批模式。返回新任务显示工作区默认设置，恢复任务显示该任务已保存的设置。

Web 运行时默认展开处理过程：按事件顺序直接显示完整进展、思考与轻量工具调用行，长进展不再二次折叠，成功工具也逐行可见；活动过程随页面滚动。当前思考步骤及所有仍在执行的工具文字使用流光，已结束步骤停止流光，待审批时暂停过程流光；系统减少动态效果或强制颜色模式下显示静态可读文字。用户可以在运行期间手动收起，此时显示最新进展的短预览和所有正在执行的工具。成功结束时自动收起，即使运行期间曾手动展开；仅保留正文和简洁的耗时回看入口，点击后可重新查看完整过程。失败、中断默认展开，等待补充信息默认收起并保留独立澄清入口。压缩与后台事件放在末尾的运行事件中。已知内置搜索、网页阅读、文件读取与命令输出优先展示可读结果，原始输入与结果仍可切换查看；未知工具保持原始文本。尚未返回结果的工具明确显示等待结果，截断和分页结果明确提示可用片段。完成后展开的过程及长工具结果使用有界阅读区域，长工具名称和路径可换行。新待审批动作会重新展开，但确认卡片独立于过程折叠，始终可操作。完成后的“思考了”入口使用 Runtime 保存的本轮实际耗时，包含模型、工具执行及等待，不表示纯模型推理时间；没有计时数据的旧记录不显示时长。工具调用数与失败记录数保留在入口提示中。运行状态通过无障碍状态区域播报；结束时若焦点位于收起的过程或消失的当前工具预览，返回过程摘要。

回答下方的“来源与链接”合并本轮成功检索、已阅读网页片段与 Markdown 正文链接，按页面 URL 去重；正文链接不自动代表已检索、已阅读或已核实证据。悬停或聚焦网页链接可预览标题、站点和可用摘要，普通点击打开来源面板；组合键点击仍直接打开原网页。面板可筛选正文链接或已阅读片段，并提供原网页入口。长回答提供带可跳转目录的完整阅读视图；工作区 Markdown 文档预览也提供目录。

实时语音开启后可点击“展开语音视图”进入专注浮层，手机使用完整视图。收起只退出视觉，结束通话是独立操作；静音、打断、设备选择、字幕和工具审批保持可用。形体通过真实几何、材质和灯光绘制，只响应本地可采样的实际音量，浏览器朗读只有播放状态。减少动态效果或渲染不可用时保留静态视图与全部操作；离屏和后台停止视觉绘制/采样，媒体连接继续。Live 未启用时不增加入口。

工作区文档预览/下载的安全读取目前仅支持 macOS/Linux；Windows 返回明确的不支持提示。
它不会为兼容平台而放宽符号链接、特殊文件或大小限制。上传图片预览是独立 Interface，不受此限制。

普通模式仅在当前运行有计划时，在输入框上方居中显示进度胶囊；停止、失败或运行结束后消失。步骤序号、当前步骤和完成计数来自实际计划；点击向上打开非模态清单，Esc、关闭按钮、点击外部或移出焦点关闭，输入区保持可用。新问题未产生计划时不会沿用上一轮的进度。步骤结束不代表整个运行完成。完成后的对话提供默认折叠的“查看本轮计划”，复用已记录的计划状态；页头和右侧不再保留独立计划面板。`/tasks` 是打开运行记录的兼容入口，原始计划仍持久化以供恢复、审核与审计。

Plan Mode 探索时显示“正在探索并规划”。方案提交审核后，完整步骤进入对话正文，输入框上方提供“确认并开始执行”和“先调整方案”。修改入口聚焦意见输入框；提交失败保留反馈。确认和修改都携带当前方案 revision 调用同一 Host 审核 Interface，确认成功切换到直接执行，修改成功保留先规划模式；等待请求期间防止重复提交，切换任务后丢弃旧响应的界面更新。方案确认与工具审批设置独立。

桌面侧栏收起后保留窄图标栏，可展开、新建任务或搜索已有任务；展开状态和收起偏好在刷新后保持。点击搜索打开独立弹框并聚焦无边框输入区，不展开侧栏或清空草稿。空输入展示最近的未归档对话，输入关键词按标题筛选全部对话（包括带标记的归档对话）；目录中的异步标题更新会同步到结果。方向键选择、Enter 打开对话，中文输入法确认候选不会误打开；`Esc`、关闭按钮和点击遮罩关闭弹框。列表独立滚动，Tab 焦点限制在弹框内，关闭后返回搜索入口。手机侧栏使用遮罩和焦点约束，点击搜索时关闭侧栏，搜索关闭后焦点返回展开按钮；手机开关不改变桌面偏好。侧栏只提供当前已经实现的任务功能。

模型菜单与输入框等宽，并按可用空间显示在其上方或下方；搜索直接融入菜单，仅列出实际配置的模型。方向键选择、Enter 确认、Esc 关闭；中文输入法确认候选不会误切换。打开或关闭菜单不带动页面滚动，输入框聚焦时保留轻边线。

Web 对话滚动条位于页面右缘，正文保持居中阅读宽度。长对话在宽屏右侧显示消息位置标记；悬停或聚焦预览消息，点击跳转，方向键及 Home / End 可逐条定位。回看历史时暂停跟随输出，点击“回到底部”恢复跟随；窄屏保留原生滚动。

未命名对话接受输入后先显示“新对话”，Host 在后台用当前模型生成短标题，正文立即开始输出。标题完成后侧栏、页面标题和浏览器标题自动更新，不重载消息或清空草稿。生成失败或超时会回退到脱敏后的输入摘要；手动命名（包括命名为“新对话”）始终优先。标题请求不调用工具，不作为对话 turn；待生成状态记录在 Session，重启后可恢复。TUI / headless 通过相同 Host 使用同一目录标题。

模型将 `<think>` / `<thinking>` 混在普通文本中时，Web 会把其中正文归入处理过程，移除控制标签；单独的关闭标签也会被识别。流式未完成标签不会闪现在正文，历史回放使用同一投影。原生 thinking 段不会因为闭合标签变成最终回答，其 Markdown 状态不会污染下一文本通道或用户轮次。代码块、行内代码和转义示例保留原文，Session 与 provider 消息不改写；回复复制及 `/copy` 使用已投影的回答。运行记录的标准视图也使用这份投影进行显示、搜索和复制，详细视图保留原始协议记录；工具输出不会被当作模型思考处理。

运行时，输入框右侧以圆形方块按钮停止生成；停止请求处理中显示等待状态，失败后保留运行和重试入口。“立即补充 / 完成后继续”始终显示当前选择，空草稿不显示无效的发送按钮；输入补充内容或添加附件后显示发送队列按钮。输入提交后标记“已排队”，只有收到 Host 的送达事件才显示“已送达当前任务 / 已送达下一轮”；撤回不会显示送达回执，开始新运行或切换任务时清除临时回执。

图片可通过 `+`、粘贴或拖放添加；每条消息最多 8 张，每张上限 20 MiB，支持 PNG、JPEG、GIF、WebP。上传中禁止发送，批量上传部分失败时保留成功项并提示重新添加失败项。草稿显示可移除的缩略图，发送后及重新加载 Session 后，用户消息气泡内部显示图片，可点击放大、按 Esc 关闭，读取失败可重试。`run.started` 实时事件携带附件引用；队列输入和重试沿用相同事件契约，旧事件缺少附件字段时仍可读取。图片回答与文字回答共用增量输出链路，收到文字片段即显示，无需等待完成事件；首段到达时间取决于供应商。预览通过 Host 校验内容寻址引用、大小和图片签名，不依赖临时浏览器缓存。普通 PDF、Office、文本等文件尚不支持作为上传附件；`@文件` 引用和工作区文档预览是独立能力。

用户消息下方提供复制与编辑，鼠标悬停、键盘聚焦或触屏时可见。编辑区支持取消、`Esc` 返回、`Ctrl/Cmd+Enter` 发送、不修改文本直接重新生成，以及失败后保留草稿重试。发送后，Host 在**当前 Session** 追加活动历史回退 marker，并从被编辑消息之前的前缀重新运行；Session ID、标题、当前窗口和主输入框草稿不变。被替换消息及后续回答、工具 transcript、旧压缩摘要、Session resource snapshot 与仅由该后缀支持的自动记忆不会进入新模型上下文，但原始 journal 仍保留审计证据。普通消息保留原附件，Skill/Prompt 调用沿用对应 Host Interface；旧方案的审核授权不会沿用到替换消息。已发生的工作区与外部副作用不会回滚。活动运行期间先停止或等待结束后再编辑。检查点的“分支”操作仍会显式创建新 Session。

存在待核实的外部操作时，Host 在启动或创建编辑分支前拒绝受理，保留原对话和编辑草稿。
Web 显示“查看并处理”：逐条检查结果，填写核实依据后记录人工确认；请求失败会保留输入。
该确认只作用于选中的历史操作，不重试调用，也不修改后续权限。完成门禁不会要求模型把
effect ID 写进产物。MCP 工具缺少 effect 声明则在执行前返回 `effect_contract_required`，
应由操作者按真实语义配置 `tool_effects`，不能从 `tool_risks: read` 自动推导。

失败任务可以直接归档或删除，无需为了清理列表而确认未知结果。删除只追加不可撤销的目录墓碑，
保留原始对话、产物和待核实记录；它不撤销文件修改或外部动作，也不代表任务成功。
重复删除同一任务返回成功。工作区仍在执行、任务存在活动 Agent 或未结束的语音连接时，
需要先结束执行；归档任务恢复可见性后，继续执行仍受原有恢复检查约束。

通过 `/plan` 选择的规划模式与“每次确认”等审批选项是两个独立设置。兼容命令 `/mode plan` 设置 collaboration mode，`/mode manual`、`/mode accept_edits` 同时回到 Default；直接点击审批选择器只修改审批策略。计划清单是只读投影，真正执行仍通过带 revision 的方案确认与原有工具审批 Interface。

Web 模型选择器使用 LobeHub 模型品牌图标；未知名称显示通用图标，品牌匹配不参与模型能力判定。网页链接从目标 origin 的 `/favicon.ico` 加载站点图标，不发送引用页信息；加载失败显示通用网页图标。本地文件链接继续使用工作区文档预览。

Web 回复中的工作区文档链接与内联文件路径可点击预览；本轮成功的 `write_file` / `edit_file` 还会生成去重文件卡片。预览通过 Host 读取当前文件，支持 Markdown 阅读/源码切换、HTML 隔离静态预览、纯文本、常见图片、浏览器 PDF、Word（`.docx`）静态阅读，以及 Excel（`.xlsx`）、CSV 和 TSV 只读表格，并提供下载。表格支持工作表切换；为保证浏览器流畅度，每个工作表最多显示前 500 行、50 列，工作簿最多显示前 20 个工作表，原文件不受影响。HTML 预览会在资源与大小上限内安全内联工作区相对 CSS、图片和字体；HTML 与 Word 预览仍禁止脚本、远程资源和跳转，SVG 按源码显示。PowerPoint、旧式 Office 格式、转换失败的 Office 文件及超过 1 MiB 的文本保留原文件下载，单文件读取上限 20 MiB；不支持路径逃逸、隐藏路径、符号链接或特殊文件。删除、移动和超限等读取失败可重试。此入口展示工作区当前内容，不声称是历史快照；原有产物写入路径规则不变。

## 4. 快捷键与发现入口

- TUI：`Ctrl+P` 打开命令面板，`Alt+C` 等同 `/copy`，`Shift+Tab` 循环审批模式。
- TUI：`Alt+P` 打开模型选择器；`Alt+M` 打开模式选择器；`Alt+T` 等同 `/tasks`。计划聚焦时 `Enter` / `Space` 折叠步骤，`E` 切换说明。
- TUI/Web：输入 `/` 打开 slash 菜单；输入 `@` 打开文件 mention 补全。
- TUI：整段输入的 `/model`、`/mode`、`/tasks` 补全用 `Enter` 直接打开，`Tab` 只补全；其他命令保留原有补全行为。
- Web：完整命令用 `Enter` 或点击立即执行，带待填参数的命令先补全；`Tab` 只补全，`Esc` 关闭菜单。中文输入法确认候选不会触发发送。
- TUI：`Esc` 依次处理关闭补全、取消 run、清空输入；空闲退出使用 `/exit` 或 `Ctrl+C`。
- Web：会话切换、恢复和新建也可直接使用左侧栏。

GLB（`.glb`）文件支持按需 3D 预览，可拖动旋转，也可使用旋转、缩放与重置视角按钮。加载或渲染失败可重新预览，下载始终保留；关闭后释放模型资源。首版仅支持有界的内嵌静态模型，外部/data/blob URI、压缩扩展、sparse、morph 和蒙皮会被拒绝，不加载外部解码器。格式/顶点/纹理解码限额见 WebUI Three.js 实施记录，尚未完成真机性能验收。

九尾狐继续默认使用现有视频。正式模型到位后，将自有 GLB 放入 `src/web/public/mascot/`，构建时设置 `NEXT_PUBLIC_MASCOT_RENDERER=three` 和 `NEXT_PUBLIC_MASCOT_MODEL_URL=/mascot/fox.glb` 可选用模型路径。模型须内嵌资源，骨骼和动画仍受限额约束；支持命名 `idle` 和 `react` / `reaction` / `wave` 的动画，缺少动作时不伪造骨骼效果。未配置模型、减少动态效果或加载/渲染失败会回退现有视频/静态路径；正式角色资产及画质待验收。

本地开发的 `/scene-preview`（静态导出 `/scene-preview.html`）用于检查语音场景和 GLB 示例，所有状态/RMS/审批均标注为模拟，不申请麦克风或建立通话。

## 5. 维护约定

- TUI 命令必须先更新 `src/lumen/ui/slash_commands.py`，不要在 help、补全或 command gate 中另建手写清单。
- Web 新命令必须同步更新 `src/web/src/lib/slash-commands.ts`、前端 handler、API client/schema 与 `/help` 文本。
- CLI 选项以 Typer 定义和 `lumen --help` 为准；修改后同步更新本文件与 README。
