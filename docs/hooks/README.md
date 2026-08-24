# Lumen Hooks

`hooks` 按配置顺序串行运行，首个 deny 会短路。支持事件：

- `user_prompt_submit`：模型收到 prompt 前，可拒绝或改写。
- `pre_tool_use`：工具执行前，可拒绝或通过 `modified_args` 改参数。
- `post_tool_use`：工具完成后，可通过 `modified_result` 改写返回给模型的内容。
- `stop`：成功 run 发出 `RunCompleted` 前触发。
- `notification`：供通知适配器使用。

Command hook 使用 argv 列表直接执行，不启用 shell；工作目录是 workspace，stdin 是
UTF-8 JSON：

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

退出码 `0` 表示继续，`2` 表示拒绝，其他非零退出码和超时也会阻止本次操作。
退出码为 `0` 时，stdout 可为空，或输出一个 JSON decision：

```json
{"allow": true, "modified_args": {}, "modified_result": null, "modified_prompt": null, "reason": ""}
```

Python hook 配置为 `module` + `factory`。目标 callable 接收 `HookContext`，可同步或
异步返回 `HookDecision`、同字段 mapping 或 `None`。导入失败会在启动时 fail fast；
运行时异常会记录 diagnostic 并默认放行。使用 `/hooks` 查看注册项、最近触发时间和 deny 次数。
