# Sandbox、MCP 发现与 Agent harness 失效分析

日期：2026-09-03。依据：当前工作区源码、当前配置的脱敏字段、本机最小复现和契约测试。

## 结论

截图反映的是多个问题叠加，不能归结为模型弱，也不能通过关闭沙箱解决：

1. `sandbox.network: false` 是当前配置明确选择的子进程网络限制，本身按设计工作。
2. Seatbelt 缺少正常运行系统工具所需的读取权限，导致 curl/Python 在真正联网前就失败。
3. 延迟工具注册成功，但模型实际收到的发现入口缺少工具目录，降低了 Exa 的可发现性。
4. 搜索只接受 ASCII 词元，中文查询可能得到空词集；缺少语言无关的浏览恢复路径。
5. 指令与命令输出未充分说明命令沙箱和 MCP 的不同权限作用范围，给错误归因留下空间。
6. MCP 恢复原来会盲目重试任何工具，虽然看似更有韧性，却可能重复外部副作用。

本轮修复作用在 SandboxRunner、现有工具 Interface、AgentRuntime 和 MCP Adapter；没有增加第二套
能力注册、生命周期或调度权威，也没有重写 LumenAgentLoop。

## 证据与适用范围

本工作区没有找到截图所指的 `outputs/threejs-3d-web-ecosystem-research.md`，在本工作区及用户级
Session 目录中也未找到该文件名对应的历史记录。因此不能断言历史那次 run 的 Exa 连接一定成功，
也不能还原它当时是否调用过搜索。以下是当前代码的确定缺陷与本机可复现行为，不冒充历史取证。

仅读取当前配置的 sandbox 策略、MCP 名称与工具声明，未输出 endpoint、headers、env 或凭据。
当前项目及用户配置均有 Exa，声明 `web_search_exa`、`web_fetch_exa` 为 read；沙箱配置均为
workspace_write、network=false。通过已有 MCP Interface 做只读 list_tools 检查成功，返回了
`web_search_exa`、`web_fetch_exa` 和 `agent_run`。这只证明检查当时发现连接正常，没有调用 agent_run，
没有验证搜索内容质量，也没有修改用户配置。

## 执行链路及问题位置

```text
ResourceManager
  ├─ SandboxConfig → SandboxRunner → run_command / run_skill_script
  ├─ McpServerConfig → MCP transport → 工具描述符 → CapabilityGateway
  └─ SkillLoader → compact catalog → load_skill / read_skill_resource
                                      ↓
AgentRuntime → Provider 可见 Schema / search_tools → LumenAgentLoop
                                      ↓
CapabilityGateway → policy / hooks / approval → 工具执行与 effect receipt
                                      ↓
ContextEngine / append-only Session / CompletionGate
```

### 1. “沙箱禁止所有联网”的推导不成立

`SandboxRunner` 在 macOS 使用 deny-default Seatbelt profile，network=false 时不添加 network allow；
Linux 用 bubblewrap 的 network namespace，只有 network=true 才 share-net。

MCP 的 Streamable HTTP / stdio transport 由 `build_mcp_toolset` 创建，不经命令 SandboxRunner。
内置 web 工具也通过独立的 URL、SSRF 和审批约束访问外网。因此该配置是“模型触发的受限子进程不能
联网”，不是“Host 的所有网络能力都不可用”。模型 provider 自身的 HTTP 请求同样不是命令沙箱内 curl。

两个不同问题必须分别诊断：用户明确拒绝的动作不能换工具绕过；某一执行路径的技术故障则应让模型
寻找仍被独立授权的能力。此处 Exa 正是需要发现并评估的另一个 Interface。

### 2. macOS 工具启动失败有精确复现

补丁前，使用默认 SandboxConfig：

| 探针 | 观察结果 | 含义 |
|---|---|---|
| `/usr/bin/curl --version` | exit=1，读取 `/private/etc/ssl/openssl.cnf` 被拒绝 | 离线操作也失败，不能归因于出站网络 |
| `/usr/bin/python3 -c ...` | exit=1，xcode-select 无法读取 developer_dir 路径 | 开发工具定位被阻断，不是 Python 安装损坏 |
| 当前 Lumen Python `import ssl` | exit=0 | 同一沙箱下不同启动链有不同依赖 |

原有 read roots 包含系统库、工作区和当前 Python，但遗漏公共 TLS 配置、开发工具链接 metadata；
独立 HOME/TMP 也只获写权限，未完整获得回读权限。修改限定为必要公共配置、证书、路径 metadata
和必要设备，未开放整个系统配置目录或用户 HOME。额外修复了可执行文件解析失败时临时目录未清理的问题。

修复后 curl --version 和两种 Python 探针都成功。network=false 下 curl 访问外网仍失败；
本机 TCP 测试验证 network=false 拒绝连接、network=true 允许连接。系统 Python 仍可能输出
xcrun 公共缓存写入被拒绝的非致命信息；没有为了消除该提示而开放共享缓存目录。

### 3. 延迟工具的“可搜索”不等于模型“知道它存在”

`ResourceManager.open()` 已完成 list_tools、命名、风险分类、Schema 保存及 Gateway 注册。
`defer_tools` 默认 true，只有 always_load_tools 常驻。

问题发生在 `AgentRuntime._lumen_tool_schemas()`：它过滤未发现的描述符，随后仅添加一个泛化的
search_tools Schema。Context assembler 虽有 compact catalog 的代码，但 Runtime 传入的已经是
过滤后的集合；且预算/观测中的 catalog 不能替代真正送给 Provider 的工具目录。

修复把有界名称/用途目录写入 search_tools 的实际 description。它由当前 Gateway 投影，因此
遵循子 Agent 工具收窄；Schema 依然延迟加载。目录预算约 6,000 字符，每条描述最多 180 字符；
超出部分通过浏览发现，不全量加载远端参数 Schema。远端描述标为外部元数据，不提升为系统指令。

词法搜索现在保留 Unicode，匹配名称、来源和描述；空查询或星号返回按名称排序的下一批 10 个
未加载工具。纯中文查询不会再被强制丢弃，但这不是跨语言语义搜索：中文与全英文工具描述仍可能
不匹配，模型可以依靠可见名称与浏览恢复。没有引入额外 embedding provider 或硬编码 Exa 选择规则。

### 4. 失败恢复需要证据，也需要限制

基础指令要求时效性调研优先使用检索/抓取工具，在 block/skip 前评估授权替代能力。
run_command 的 description 和结构化结果提供实际 mode、网络策略及其命令作用范围，还提示当前
可用 Python 路径和 Skill 专用读取 Interface。Runtime 提供 MCP 启动状态快照，使 optional
Server 启动失败不再完全消失在模型视野之外；它不承诺 Server 永远在线。

没有把每次非零退出码强制等同于整个 run 失败：测试、探测等本来就可能使用非零状态作为数据。
命令结果只证明执行过程及返回结果，不能证明任意命令的全部文件副作用已捕获，也不能证明研究完整。

`CompletionGate` 对 Default 计划允许显式 blocked/skipped 步骤终结，这是呈现部分结果所需的行为。
因此仅靠 gate 无法判断“是否已经尝试所有合适的联网工具”。本轮没有添加自然语言黑名单或凭空猜测
任务意图的完成门禁。提高能力可见性和反馈质量后，仍需真实模型行为评估才能量化任务成功率。

### 5. MCP 的安全重试必须按 EffectKind 决定

旧 `ResilientMcpToolset` 在传输异常后重连并重发任何工具。断开可能发生在远端已执行、响应未送达时，
对发邮件、创建记录或未知工具重发会产生重复副作用。

现在仅配置明确声明 `tool_effects: observe` 的工具允许自动重连重试一次；其他 effect 返回
`mcp_outcome_unknown`，提示先核实远端结果。Risk=read 不自动等价于 EffectKind=observe。
当前 Exa 配置只有 tool_risks，没有 tool_effects，因此仍保持 unknown 的状态追踪与保守恢复。
对于已审核确认为只读的工具，操作者可另外声明 observe；本轮没有修改含凭据的用户配置。

审核搜索与抓取工具的语义后，可在既有 Exa 配置中补充下列独立声明（不适用于 `agent_run`）：

```yaml
mcp_servers:
  exa:
    tool_effects:
      web_search_exa: observe
      web_fetch_exa: observe
```

这是既有配置的局部补充示例，不是完整配置文件。strict 模式下仅有 read 风险声明的成功调用，
仍可能生成 reconciliation_required 并阻止完成；不能为了让流程通过而从 Risk 自动推导 EffectKind。

## 保持不变的契约

- Gateway、PermissionPolicy、Hook 与审批仍拥有同一执行裁决；发现不授权执行。
- workspace_write fail-closed，默认命令网络禁用，路径逃逸防护及 `.git` / `.lumen` 写保护保留。
- Skill 通过已发现名称与相对路径读取，不开放全局 Skill 目录给任意 shell。
- 未声明 MCP 风险/副作用保持 external_unknown/unknown；MCP transport 不再自动重放未知外部调用。
- Session 不改写历史；本轮无配置/Session schema 升级，无新持久状态。
- TUI、Web、headless、Default 与 Plan 共用 Runtime/工具路径，没有加入客户端专属逻辑。
- 工作区原有大量 staged/unstaged 修改保留；没有提交、推送或发布。

## 删除与替换依据

未删除 Module 或公开 Interface。ASCII-only 分词被 Unicode 匹配替代，旧的纯泛化搜索说明被当前
Gateway 目录替代；MCP 无条件重发分支被按已声明 EffectKind 的重试条件替代。这些都是同一权威
内部的重复/不完整行为替换，不保留两套调度逻辑。

## 验证

测试覆盖真实 Seatbelt 下系统运行时、临时文件回读、外部文件拒读、journal 拒写、网络开关；
模型请求层覆盖中文/英文/来源搜索、目录可见性、ContextEngine 开关、无匹配后分页发现、
子 Agent 收窄和发现后审批拒绝；MCP 覆盖 observe 恢复与未知动作断连后不重放。

最终执行结果：

- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- `uv run python -m lumen.contracts --check`：通过。
- `uv run python scripts/build_architecture_atlas.py --check`：重新生成后通过。
- `git diff --check`：通过。
- `uv run pytest`：943 passed，1 failed，60 个 TUI snapshot 全部通过；包含 Host/Web API 契约测试。
- 唯一失败为 `tests/test_config.py::test_project_config_uses_expected_model_registry`：测试硬编码
  `omlx-qwen3.8-27b-4bit`，本地配置实际包含 `qwen3.8-flash` / `qwen3.8-max`。本轮未修改该测试
  或本地模型列表，没有通过改动用户配置消除失败。

未修改 Web、打包入口或 API schema，因此没有运行前端构建或 wheel 构建；Linux bubblewrap 未做
真实 OS 集成验证。脚本模型测试验证 harness 契约，不代表已证明真实模型一定选用 Exa；本次也未
重新执行原 three.js 研究任务。
