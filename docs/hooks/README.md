# Lumen Hooks

`hooks` 按配置顺序串行运行，首个 deny 会短路。支持事件：

- `user_prompt_submit`：模型收到 prompt 前，可拒绝或改写。
- `pre_tool_use`：工具执行前，可拒绝或通过 `modified_args` 改参数。
- `post_tool_use`：工具完成后，可通过 `modified_result` 改写返回给模型的内容。
- `stop`：成功 run 发出 `RunCompleted` 前触发。
- `notification`：供通知适配器使用。

Command hook 使用 argv 列表直接执行，不启用 shell；工作目录是 workspace，并复用当前项目
`SandboxRunner` 的文件、网络、环境、超时、输出和进程树约束。工作区外只有 command argv
明确引用且已存在的绝对路径会增加为只读资源，不会把整个配置目录暴露给进程。stdin 是 UTF-8 JSON：

```json
{
  "event": "pre_tool_use",
  "session_id": "...",
  "workspace": "/project",
  "tool_name": "run_command",
  "tool_args": {"command": "pytest"},
  "tool_result": null,
  "prompt": null
}
```

退出码 `0` 表示继续，`2` 表示拒绝，其他非零退出码、超时、启动错误或 sandbox
不可用也会 fail closed 并阻止本次操作。
退出码为 `0` 时，stdout 可为空，或输出一个 JSON decision：

```json
{"allow": true, "modified_args": {}, "modified_result": null, "modified_prompt": null, "reason": ""}
```

Python hook 配置为 `module` + `factory`。目标 callable 接收 `HookContext`，可同步或
异步返回 `HookDecision`、同字段 mapping 或 `None`。导入失败会在启动时 fail fast；
Python hook 是操作者显式配置的进程内受信代码，不受子进程 OS sandbox 包裹。
运行时异常会记录 diagnostic 并默认放行。在唯一的 `CapabilityGateway` 路径中，pre hook 修改后的参数先进入
审批与单调 `ToolGuard`；post hook 只改返回模型的文本投影，不改 canonical output、客户端展示或 effect
receipt。若公开 pre-invoke Adapter 整体意外抛出，Gateway 会 fail closed。使用 `/hooks` 查看注册项、最近
触发时间和 deny 次数。
