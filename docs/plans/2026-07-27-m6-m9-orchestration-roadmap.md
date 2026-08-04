# Lumen M6–M9 实施路线图：编排广度突破计划

> **状态说明**：这是历史设计文档，保留用于解释当时的目标与决策；当前实现状态以 `docs/architecture-guide/` 和源码为准。

> **目标**：将 Lumen 综合评分从 7.9 提升至 ~8.4，把"编排广度"维度（当前 5.0，唯一 ≤6 项）补齐到 Claude Code（9.5）的可竞争区间，同时推进 Skill 生态（7.5→8.5）和 MCP 完整度（7.5→8.5）。
>
> **核验依据**：本计划基于对 `src/lumen/` 全部核心模块的源码逐行通读（runtime.py:574 的 `parallel_tool_call_execution_mode("sequential")` 硬编码、registry.py:114 的 `sequential=True`、skills.py 全文无 subprocess、mcp_tools.py 仅 tools 协议等关键事实均已验证）。

---

## 总览：四个里程碑的依赖与排序

```
M6 并行工具执行 ──┐
                  ├─► M7 钩子系统 ──► M8 Skill 脚本执行 ──► M9 MCP 协议补齐
config schema ◄───┘     (复用 M6 的批量审批)   (独立)            (独立)
```

| 里程碑 | 周期估计 | 改动文件数 | 新增/改动 LOC | 测试数 | 综合分预期收益 |
|--------|----------|-----------|--------------|--------|---------------|
| **M6** 并行工具 | 1–1.5 周 | 6 核心 + 4 测试 | ~350 | +12 | +0.3~0.5 |
| **M7** 钩子系统 | 1.5 周 | 4 核心 + 3 测试 | ~500 | +15 | +0.4~0.6 |
| **M8** Skill 脚本 | 1 周 | 2 核心 + 3 内置 skill | ~300 | +8 | +0.2~0.3 |
| **M9** MCP 补齐 | 2 周 | 3 核心 + 2 测试 | ~600 | +10 | +0.3~0.4 |
| **合计** | **~5.5 周** | **~15 核心** | **~1750** | **+45** | **7.9 → ~8.4** |

**关键原则**：
1. **零回归优先**——每个里程碑都默认保持旧行为（M6 默认串行、M7 默认无钩子），新能力是可选 opt-in。
2. **测试驱动**——沿用项目 pyright strict + ruff + pytest-textual-snapshot 的质量门，每个 M 测试数 ≥ 8。
3. **沿用 M0–M5 的命名与 commit 风格**（`M6: ...` / `M6 (tui): ...`）。

---

## M6 · 并行工具执行（编排广度第一刀）

### 6.1 现状与问题

**根因**（已源码核实）：
- `src/lumen/runtime.py:574` 写死 `with self.agent.parallel_tool_call_execution_mode("sequential"):`
- `src/lumen/tools/registry.py:114` 每个工具都 `sequential=True`
- 后果：模型即使请求"并行读 5 个文件"，pydantic-ai 也会强制串行成 5 轮，延迟 ×5、token ×5

**这是 Lumen 与 Claude Code（Agent 架构 8.0 vs 9.5、编排广度 5.0 vs 9.5）最大的单一差距源**。

### 6.2 设计：三档可配，默认串行保证零回归

```python
# config.py — LimitsConfig 新增字段
class LimitsConfig(StrictModel):
    ...
    #: 并行工具执行策略。三档：
    #:   sequential (默认)：保持现有行为，工具逐个执行
    #:   parallel_safe：仅 READ 风险工具并行；任何 WRITE/EXECUTE 仍串行
    #:   parallel：全部并行（含写/执行），需更严格的审批聚合
    parallel_tool_calls: Literal["sequential", "parallel_safe", "parallel"] = "sequential"
```

**三档语义**（关键设计：`parallel_safe` 是甜点档）：

| 档位 | READ 工具 | WRITE/EXECUTE 工具 | 审批 | 适用场景 |
|------|-----------|-------------------|------|---------|
| `sequential` | 串行 | 串行 | 逐个（现状） | 默认，零回归 |
| `parallel_safe` | **并行** | 串行 | READ 批量放行；写仍逐个 | **推荐**，读多写少的开发任务 |
| `parallel` | 并行 | 并行 | 全部批量聚合 | 高吞吐批处理，需用户信任 |

### 6.3 实施步骤

**步骤 1：config schema（config.py）**
- 在 `LimitsConfig` 新增 `parallel_tool_calls` 字段（上面已设计）
- 在 `McpServerConfig` 不需改动（MCP 工具沿用本地策略）

**步骤 2：registry.py — 工具级 sequential 标志**
```python
# registry.py: build_local_tools 改造
def build_local_tools(
    self, policy: PermissionPolicy, *, default_timeout: float,
    parallel_mode: Literal["sequential", "parallel_safe", "parallel"] = "sequential",
) -> list[Tool[None]]:
    tools: list[Tool[None]] = []
    for name, entry in self._entries.items():
        decision = policy.decide(name, entry.spec.risk)
        if decision is PermissionDecision.DENY:
            continue
        # 决定该工具是否允许并行
        allow_parallel = _allows_parallel(parallel_mode, entry.spec.risk)
        tools.append(
            Tool(
                entry.spec.function,
                name=name,
                description=entry.spec.description,
                sequential=not allow_parallel,  # ← 关键改动
                requires_approval=decision is PermissionDecision.CONFIRM,
                timeout=entry.spec.timeout or default_timeout,
                metadata={"origin": entry.origin, "risk": entry.spec.risk.value},
            )
        )
    return tools

def _allows_parallel(mode: str, risk: Risk) -> bool:
    if mode == "sequential":
        return False
    if mode == "parallel_safe":
        return risk is Risk.READ  # 只有读并行
    return True  # parallel：全部
```

**步骤 3：runtime.py — 移除硬编码 sequential**
```python
# runtime.py:574 改为读取配置
# 在 AgentRuntime.__init__ 存储 parallel_mode（从 resources 传入）
with self.agent.parallel_tool_call_execution_mode(
    "sequential" if self.parallel_mode == "sequential" else "parallel"
):
    ...
```
注意：pydantic-ai 的 `parallel_tool_call_execution_mode` 是 agent 级开关，但单个工具的 `sequential=True` 会覆盖它。所以**真正生效的是工具级 `sequential` 标志**（步骤 2），agent 级设 `"parallel"` 即可放行，具体哪些工具并行由工具标志决定。

**步骤 4：resources.py — 把策略传入 runtime**
- `ResourceManager._build_runtime` 调用 `build_local_tools` 时传入 `config.agent.limits.parallel_tool_calls`
- `AgentRuntime.__init__` 增加 `parallel_mode` 参数存储

**步骤 5：审批聚合（interactive_queue.py / 新增 approval_batcher.py）**

这是并行模式的**正确性核心**。问题：并行模式下同一轮可能同时来 5 个 `ApprovalRequest`，逐个弹卡片体验很差。

设计：新增 `ApprovalBatcher`，把同一轮的待审批请求**按 risk 分组**，生成一个 `ToolApprovalBatchPending` 事件（替代逐个 `ToolApprovalPending`）：

```python
# 新增 events.py
@dataclass(frozen=True, slots=True)
class ToolApprovalBatchPending:
    batch_id: str
    requests: tuple[ApprovalRequest, ...]  # 同批多个
    risk_summary: str  # "3×read, 1×write" 给 UI 一句话
```

UI（`approval_panel.py`）渲染为"批准全部 (3) / 逐个查看 / 拒绝全部"。`accept_edits` 模式下 READ 直接放行，只有 WRITE/EXECUTE 才聚合成卡。

**步骤 6：指标与错误恢复的对齐**
- `tool_call_count`、`start_times`、`finished_calls` 已是 dict/set 结构，天然支持并发 ✓（无需改）
- `FunctionToolCallEvent` / `FunctionToolResultEvent` 用 `call_id` 对齐，乱序到达安全 ✓
- **错误恢复**：`RecoverableToolErrors` 的 `ModelRetry` 在并行上下文只重试失败工具——需写测试确认 pydantic-ai 行为符合预期，不符则降级为"任一失败则整轮失败"（更保守）

### 6.4 验收标准（Definition of Done）

- [ ] `config.agent.limits.parallel_tool_calls` 三档均可配，YAML 校验通过（`extra=forbid` 不破坏）
- [ ] 默认 `"sequential"` 时，所有现有测试（537 例）零修改全过
- [ ] 新增测试 ≥ 12：
  - `parallel_safe` 模式下，TestModel 注入 2 个 read_file 并行 call → 单轮完成、2 个 `ToolCallFinished`
  - `parallel_safe` 模式下，1 read + 1 write → read 并行、write 串行（检查时序）
  - `parallel` 模式下，2 write → 并行完成
  - 审批聚合：同批 3 个 READ → 1 个 `ToolApprovalBatchPending` 事件
  - 错误恢复：并行中 1 个工具 ModelRetry → 只重试那一个
  - 时序快照：`parallel_safe` 下无并行 call 时，输出与 `sequential` 逐字节一致
- [ ] pyright strict 全绿、ruff 全绿
- [ ] 文档：`README.md` 增加 `parallel_tool_calls` 说明；`agent.example.yaml` 加注释样例

### 6.5 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 并行模式下审批 UI 竞态 | 中 | 中 | `ApprovalBatcher` 单点串行化，所有 emit 走同一个 `asyncio.Lock` |
| pydantic-ai 并行 + ModelRetry 行为不符预期 | 中 | 高 | 步骤 6 先写探针测试；不符则 `parallel` 档降级为"首失败即停" |
| TUI 快照测试大面积失效 | 低 | 低 | 快照测试用 `sequential` 档跑（默认），并行行为用独立快照集 |

---

## M7 · 钩子系统（编排广度第二刀 + 生态入口）

### 7.1 现状与价值

**现状**（已核实）：Lumen 无任何 Pre/PostToolUse / Stop 钩子。审批门（`command_gate.py`、`registry.py:PermissionPolicy`）是硬编码的单一策略。

**价值**：钩子是 Claude Code 整个插件/权限/memory/telemetry 生态的**标准入口**。没有钩子，Lumen 永远只能"自己实现一切"，无法让社区扩展。这一项做完，Skill 维度和 MCP 生态维度的可扩展性都会跟着提升。

### 7.2 设计：事件总线 + 配置化钩子

**钩子事件类型**（对齐 Claude Code 命名，降低迁移成本）：

```python
# 新增 src/lumen/hooks.py
class HookEvent(StrEnum):
    PRE_TOOL_USE = "pre_tool_use"      # 工具调用前：可 allow/deny/改参数
    POST_TOOL_USE = "post_tool_use"    # 工具调用后：可改结果、记日志
    USER_PROMPT_SUBMIT = "user_prompt_submit"  # 用户提交前：可改写/拒绝
    STOP = "stop"                       # 一轮 run 结束：记日志、触发后台任务
    NOTIFICATION = "notification"       # 需要通知用户时

@dataclass(frozen=True, slots=True)
class HookContext:
    event: HookEvent
    tool_name: str | None
    tool_args: dict[str, Any] | None
    tool_result: str | None
    session_id: str
    workspace: Path

@dataclass
class HookDecision:
    allow: bool = True
    modified_args: dict[str, Any] | None = None  # pre_tool_use 可改参数
    modified_result: str | None = None            # post_tool_use 可改结果
    reason: str = ""
```

**钩子类型**（两种，覆盖 Claude Code 能力）：

| 类型 | 触发 | 能力 | 示例 |
|------|------|------|------|
| **Command hook** | 事件匹配 | 运行外部命令（stdin 传 JSON context），按 exit code 决定 allow/deny | `pre_tool_use: run_command` → 跑 `scripts/block-rm-rf.sh` |
| **Python hook** | 事件匹配 | 调用本地 Python 函数（插件机制复用） | `post_tool_use: write_file` → 自动 `ruff format` |

### 7.3 配置 schema（config.py）

```python
class HookConfig(StrictModel):
    event: Literal["pre_tool_use", "post_tool_use", "user_prompt_submit", "stop", "notification"]
    #: 工具名匹配（glob），仅 pre/post_tool_use 有效。空 = 全部。
    matcher: str = "*"
    #: 命令钩子：subprocess，stdin 传 JSON，exit 0=allow / 2=deny / 其他=block
    command: list[str] | None = None
    #: Python 钩子：复用插件机制 module:factory
    module: str | None = None
    factory: str = "hook"
    #: 超时秒数
    timeout: float = Field(default=10.0, gt=0)

class AppConfig(StrictModel):
    ...
    hooks: list[HookConfig] = Field(default_factory=list[HookConfig])
```

`agent.example.yaml` 示例：
```yaml
hooks:
  - event: pre_tool_use
    matcher: "run_command"
    command: ["bash", ".lumen/hooks/block-dangerous.sh"]
  - event: post_tool_use
    matcher: "write_file"
    module: my_hooks.autoformat
    factory: format_after_write
```

### 7.4 实施步骤

**步骤 1：新增 `src/lumen/hooks.py`（核心模块 ~250 行）**
- `HookEvent`、`HookContext`、`HookDecision` 数据类
- `HookRegistry`：按 event 索引，`match(tool_name, pattern)` 用 fnmatch
- `CommandHookRunner`：subprocess.run，stdin=JSON(context)，解析 exit code（0/2/其他）
- `PythonHookRunner`：importlib 复用 `load_plugin_specs` 的模式
- `HookBus.dispatch(ctx) -> HookDecision`：串行调用所有匹配钩子，短路于首个 deny

**步骤 2：runtime.py 接入点（4 处）**
```python
# pre_tool_use：在 FunctionToolCallEvent 处理后、实际执行前
elif isinstance(event, FunctionToolCallEvent):
    ...
    # M7: pre_tool_use 钩子
    decision = await self.hooks.dispatch(HookContext(
        event=HookEvent.PRE_TOOL_USE, tool_name=..., tool_args=..., ...
    ))
    if not decision.allow:
        # 构造一个 ToolDenied 结果直接回灌，不执行真实工具
        ...

# post_tool_use：在 FunctionToolResultEvent 处理后
elif isinstance(event, FunctionToolResultEvent):
    ...
    # M7: post_tool_use 钩子
    decision = await self.hooks.dispatch(HookContext(
        event=HookEvent.POST_TOOL_USE, tool_name=..., tool_result=content_str, ...
    ))
    if decision.modified_result:
        content_str = decision.modified_result  # 改写结果再 emit

# user_prompt_submit：在 run() 入口，prompt 进 agent 前
# stop：在 RunCompleted/RunFailed emit 前
```

**关键设计**：pre_tool_use 的 deny **不抛异常**，而是构造一个 `ToolDenied` 结果回灌给模型，让模型自己决定下一步（符合 Lumen 现有的"feed back, don't editorialise"哲学）。

**步骤 3：resources.py — 构建 HookRegistry**
- `ResourceManager.__init__` 解析 `config.hooks` → 构建 `HookRegistry`
- 传入 `AgentRuntime`

**步骤 4：MCP 工具的钩子（mcp_tools.py）**
- MCP 工具走 pydantic-ai 的 `approval_required` 回调，在回调里插入 pre_tool_use 钩子
- 这样 MCP 工具也能被钩子拦截/改写

**步骤 5：TUI 钩子状态可见（ui/commands.py）**
- 新增 `/hooks` 命令：列出已注册钩子、上次触发时间、deny 次数

### 7.5 验收标准

- [ ] `HookConfig` schema 校验通过，`command` 与 `module` 互斥校验
- [ ] 默认空 `hooks: []` 时，所有现有测试零修改全过
- [ ] 新增测试 ≥ 15：
  - pre_tool_use command 钩子 exit 2 → 工具被 deny，模型收到 ToolDenied
  - pre_tool_use command 钩子 exit 0 → 工具正常执行
  - pre_tool_use command 钩子 timeout → 视为 block，记 diagnostic
  - post_tool_use 钩子改写 result → 模型收到改写后的内容
  - matcher glob：`run_*` 匹配 `run_command` 不匹配 `read_file`
  - Python 钩子：模块导入失败 → 启动报错（fail fast）
  - 多钩子同 event：按配置顺序串行，首个 deny 短路
  - MCP 工具的 pre_tool_use 钩子生效
  - user_prompt_submit 钩子改写 prompt
  - stop 钩子能在 RunCompleted 前执行
  - 钩子异常被捕获，记 diagnostic，不中断 run
  - `/hooks` 命令输出格式快照
- [ ] pyright strict 全绿、ruff 全绿

### 7.6 风险与缓解

| 风险 | 缓解 |
|------|------|
| 钩子阻塞 agent 循环 | 所有钩子有 `timeout`（默认 10s），超时视为 block |
| 钩子异常导致 run 崩溃 | `HookBus.dispatch` 全程 try/except，异常记 diagnostic、默认 allow |
| 命令注入（钩子命令含用户输入） | context 走 stdin JSON，不走 shell；命令列表走 exec 不走 shell |

---

## M8 · Skill 脚本执行 + 内置 Skill 生态

### 8.1 现状与价值

**现状**（已核实）：`skills.py`（452 行）有完整的发现/激活/提示词注入机制，但：
- **无脚本执行**：`scripts/` 只在 docstring 出现，全文无 subprocess 调用
- **无内置内容**：仓库内无任何自带 skill（"有机制无生态"）

这导致 Skill 维度停滞在 7.5。M8 的目标是让 skill 从"纯提示词"升级为"提示词 + 可执行脚本"，并自带 3 个高频 skill 验证机制可用。

### 8.2 设计：受限脚本执行

**安全模型**（关键，不可妥协）：
1. **白名单解释器**：只允许 `bash`/`sh`/`python`，拒绝任意可执行
2. **工作区沙箱**：脚本 cwd 锁定 skill 的 `base_dir`，禁止 `..` 越界（复用 `Workspace` 类）
3. **超时**：默认 30s，配置可调
4. **审批**：脚本执行默认走 `EXECUTE` risk，受 approval mode 约束
5. **无网络**：脚本能访问的 env 受限（剥离 API key 等）

**frontmatter 扩展**（向后兼容）：
```yaml
---
name: run-tests
description: Run the project's test suite and summarize failures
#: M8 新增：声明可执行脚本（相对 base_dir）
scripts:
  check: scripts/check.sh      # 声明存在；模型用 run_skill_script 工具调
  fix: scripts/fix.py
---
```

### 8.3 实施步骤

**步骤 1：skills.py — 解析 scripts 声明**
```python
@dataclass(frozen=True, slots=True)
class Skill:
    ...
    scripts: dict[str, Path] = field(default_factory=dict)  # M8: name→path

def load_skill(file_path, source):
    ...
    raw_scripts = frontmatter.get("scripts") or {}
    scripts: dict[str, Path] = {}
    if isinstance(raw_scripts, dict):
        for script_name, rel_path in raw_scripts.items():
            resolved = (file_path.parent / str(rel_path)).resolve()
            # 安全校验：必须在 base_dir 内
            try:
                resolved.relative_to(file_path.parent.resolve())
            except ValueError:
                warnings.append(f"skill {name}: script {rel_path} escapes base_dir, dropped")
                continue
            # 解释器白名单
            if not _allowed_interpreter(resolved):
                warnings.append(f"skill {name}: script {rel_path} has disallowed extension")
                continue
            scripts[script_name] = resolved
    return Skill(..., scripts=scripts)
```

**步骤 2：新增工具 `run_skill_script`（capability.py）**
```python
def run_skill_script(
    skill_name: str, script_name: str, args: list[str] | None = None
) -> str:
    """Execute a declared skill script in its confined base_dir."""
    skill = loader.load_skill_by_name(skill_name)
    if skill is None or script_name not in skill.scripts:
        return f"error: skill '{skill_name}' has no script '{script_name}'"
    path = skill.scripts[script_name]
    interpreter = _interpreter_for(path)  # bash → ["bash"], .py → [sys.executable]
    cmd = [*interpreter, str(path), *(args or [])]
    # 走标准 run_command 的审批 + workspace 约束
    result = subprocess.run(cmd, cwd=skill.base_dir, capture_output=True,
                           timeout=30, env=_sanitized_env())
    return _format_result(result)
```

**步骤 3：内置 3 个 skill（`src/lumen/skills/builtin/`）**
随 wheel 打包，作为 `.lumen/skills/` 的 fallback（用户可覆盖）：

| Skill | 脚本 | 价值 |
|-------|------|------|
| `commit` | `scripts/check.sh`（检查 git status + 生成 message 模板） | 验证脚本执行 + 最常用工作流 |
| `test-runner` | `scripts/run.sh`（识别 pytest/jest/go test 并跑） | 验证工作区隔离 |
| `review-pr` | `scripts/diff.sh`（git diff + 格式化） | 验证只读脚本 |

**步骤 4：内置 skill 的打包（pyproject.toml）**
```toml
[tool.hatch.build.targets.wheel]
# 包含内置 skill 静态资源
force-include = {"src/lumen/skills/builtin" = "lumen/skills/builtin"}
```
`SkillLoader` 增加第三个发现根：`importlib.resources` 定位的内置目录，优先级最低（用户/项目覆盖）。

**步骤 5：M7 钩子集成**
- skill 脚本执行前后自动触发 `pre_tool_use`/`post_tool_use` 钩子（matcher = `run_skill_script`）
- 这让用户能用钩子给所有 skill 脚本加 telemetry / 审计

### 8.4 验收标准

- [ ] `scripts:` frontmatter 字段解析，越界/非法解释器被 drop 并 warning
- [ ] `run_skill_script` 工具注册，受 EXECUTE risk + approval 约束
- [ ] 内置 3 个 skill 随 wheel 分发，`SkillLoader` 三级发现（builtin < user < project）
- [ ] 新增测试 ≥ 8：
  - skill 声明合法 script → 能执行，输出正确
  - skill script 越界（`../../../etc/passwd`）→ drop + warning
  - 非法解释器（`.exe`）→ drop
  - 脚本超时 → 友好错误
  - 脚本 exit != 0 → 错误信息回灌模型
  - env 被净化（API key 不传入）
  - 内置 skill 被用户同名 skill 覆盖
  - `/skill:commit` 手动调用能触发脚本
- [ ] pyright strict 全绿、ruff 全绿

### 8.5 风险与缓解

| 风险 | 缓解 |
|------|------|
| 脚本任意代码执行 | 解释器白名单 + workspace 锁定 + EXECUTE 审批 + env 净化（四重防御） |
| 脚本读取敏感 env | `_sanitized_env()` 仅传 `PATH`/`HOME`/`LANG`，剥离所有 `*_KEY`/`*_TOKEN` |
| wheel 未包含 skill 文件 | `pyproject.toml` force-include + CI 测试 wheel 解包后 skill 可发现 |

---

## M9 · MCP 协议补齐（resources / prompts / OAuth）

### 9.1 现状与价值

**现状**（已核实）：`mcp_tools.py`（127 行）只用 pydantic-ai 的 `MCPToolset` + `ToolDefinition`，即**只支持 tools 协议**。无 resources、prompts、roots、sampling、OAuth。

**价值**：
- **resources/prompts**：让 MCP server 能提供只读数据源和参数化模板（如数据库 schema、API 文档），大幅扩展可用生态
- **OAuth**：远程 MCP server（如 GitHub MCP）的硬门槛，没它连不上大量生产级 server

这是工程量最大的里程碑，建议放最后，且**分两阶段**（9a resources/prompts，9b OAuth）。

### 9.2 阶段 9a：Resources 与 Prompts

**设计**：resources 和 prompts 不进模型工具列表，而是作为**上下文注入源**，由用户用 `/resource`、`/prompt` 命令显式加载（避免自动注入撑爆上下文）。

```python
# config.py — McpServerConfig 新增（可选）
class McpServerConfig(StrictModel):
    ...
    #: 是否加载该 server 的 resources（默认 true）
    load_resources: bool = True
    #: 是否加载该 server 的 prompts（默认 true）
    load_prompts: bool = True
```

**实施**：
1. **`mcp_resources.py`（新增 ~200 行）**：用 fastmcp client 的 `list_resources()` / `read_resource(uri)` / `list_prompts()` / `get_prompt(name, args)`
2. **资源投影**：加载的 resources 进 ContextEngine 的一个新 zone（`mcp_resources`），有独立 token cap
3. **TUI 命令**（ui/commands.py）：
   - `/resources` → 列出所有 server 的 resources
   - `/resource <uri>` → 把该 resource 内容注入当前上下文
   - `/prompts` → 列出可用 prompt 模板
   - `/prompt <name> [args]` → 渲染并作为 user message 提交

**验收**：
- 能连接一个带 resources 的测试 MCP server（项目内建一个 fixture），`/resources` 列出，`/resource` 注入
- resource 内容进独立 zone，不挤占 history 预算
- 测试 ≥ 5

### 9.3 阶段 9b：OAuth 远程认证

**设计**：为 `streamable_http` 传输增加 OAuth 授权码流程。

```python
class McpServerConfig(StrictModel):
    ...
    #: 远程 OAuth 配置
    oauth: OAuthConfig | None = None

class OAuthConfig(StrictModel):
    client_id: str
    #: 授权/令牌端点；留空则从 server 的 well-known 发现
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: list[str] = Field(default_factory=list)
    #: 凭证存储路径（相对 sessions.directory）
    credential_file: str = ".lumen/mcp_oauth/<server>.json"
```

**流程**：
1. 首次连接 → 检测 `credential_file` 无 token → 启动本地回调 server（随机端口）→ 打开浏览器到 `authorization_url` → 收 code → 换 token → 存盘（0600）
2. 后续连接 → 读 token → 401 时用 refresh_token 续期
3. token 文件复用 `ArtifactStore` 的 0600 权限模式

**依赖**：可用 `authlib`（成熟 OAuth 库），或自己实现（PKCE 流程不复杂）。建议 authlib，减少代码。

**验收**：
- 能对 mock OAuth server 完成授权码流程
- token 持久化，重启后免重登
- refresh_token 续期生效
- credential_file 权限 0600
- 测试 ≥ 5（mock server，不真实联网）

### 9.4 阶段 9c（可选）：roots 与 sampling
- **roots**：让 MCP server 知道工作区根目录（`/roots` 通知），改动小
- **sampling**：让 MCP server 能反向请求 LLM 推理——风险高（成本/安全），**建议 M9 不做**，留作 RFC

### 9.5 整体验收标准（M9）

- [ ] resources/prompts 加载、注入、独立 zone 预算
- [ ] OAuth 授权码流程 + token 持久化 + 续期
- [ ] 测试 ≥ 10
- [ ] 文档：`agent.example.yaml` 加 OAuth server 配置样例
- [ ] pyright strict 全绿、ruff 全绿

---

## 跨里程碑：配置 schema 统一、测试策略、发布

### A. 配置 schema 演进（一次性理清）

新增字段汇总（全部有默认值，**向后兼容**）：

```yaml
agent:
  limits:
    parallel_tool_calls: parallel_safe   # M6，默认 sequential
hooks:                                   # M7，默认空 list
  - event: pre_tool_use
    matcher: "run_command"
    command: ["bash", ".lumen/hooks/guard.sh"]
mcp_servers:
  github:
    transport: streamable_http
    url: https://mcp.github.com/sse
    load_resources: true                 # M9a，默认 true
    load_prompts: true                   # M9a，默认 true
    oauth:                               # M9b，默认 null
      client_id: ${GITHUB_MCP_CLIENT_ID}
      scopes: [repo, read:user]
```

所有新字段走 `StrictModel(extra="forbid")`，旧配置零改动可用。

### B. 测试策略（贯穿 M6–M9）

| 层 | 工具 | 覆盖目标 |
|----|------|---------|
| 单元 | pytest + TestModel | 每个新模块 ≥ 8 测试 |
| 集成 | TestModel 注入并行/钩子/脚本事件 | 端到端 run loop |
| 回归 | 现有 537 测试零修改 | 默认行为不变 |
| 快照 | pytest-textual-snapshot | TUI 新 UI（批量审批、/hooks、/resources） |
| 类型 | pyright strict | 全绿 |
| Lint | ruff | 全绿 |

**关键回归保护**：每个里程碑合并前，跑 `pytest -q` 确认 537 例全过且无新失败。

### C. Commit 与发布节奏（沿用 M0–M5 风格）

```
M6: parallel tool execution with risk-tiered batching
M6 (tui): batch approval panel for parallel calls
M7: hook system (pre/post tool use, stop, prompt submit)
M7 (tui): /hooks command and hook diagnostics
M8: skill script execution with workspace confinement
M8 (skills): builtin commit / test-runner / review-pr skills
M9a: MCP resources and prompts injection
M9b: MCP OAuth authorization code flow
```

每个 commit 一个完整的、可回滚的能力单元。**M6 → M7 → M8 → M9a → M9b** 严格顺序（M7 复用 M6 的批量审批，M8 复用 M7 的钩子，M9 独立但建议靠后）。

### D. 文档更新清单

- [ ] `README.md`：新增"并行工具 / 钩子 / Skill 脚本 / MCP OAuth"章节
- [ ] `agent.example.yaml`：四个新特性的配置样例
- [ ] `docs/skills/authoring.md`（新）：skill 脚本编写规范
- [ ] `docs/hooks/`（新）：钩子事件、context JSON 格式、exit code 语义
- [ ] 修正对比报告 v2：diff 视图、日期、行号三处错误

---

## 预期收益复核

| 维度 | 现状分 | 完成后预期 | 主要贡献里程碑 |
|------|--------|-----------|--------------|
| 编排广度 | 5.0 | **7.5–8.0** | M6（并行）+ M7（钩子） |
| Agent 架构 | 8.0 | **8.5** | M6（并行工具） |
| Skill 体系 | 7.5 | **8.5** | M8（脚本+生态） |
| MCP 生态 | 7.5 | **8.5** | M9（resources/OAuth） |
| UI/UX | 8.0 | **8.5** | M6（批量审批 UI） |
| **综合** | **7.9** | **~8.4** | — |

**核心判断**：M6 是性价比最高的一项（单项 +0.3~0.5 综合分，1.5 周工作量），应立即启动；M7 是生态可扩展性的根基，必须做；M8/M9 是把"机制"变成"生态"的关键。四个里程碑做完，Lumen 从"高质量基础设施"进入"可做主力开发代理"的区间。
