# LangGraph context resume spike

> 文档复核（2026-10-10）：本文是隔离技术验证；LangGraph 不属于生产依赖。当前 Loop 与恢复契约见[架构指南](../architecture-guide/README.md)。

这个 spike 不属于生产依赖，只回答一个问题：LangGraph 的 checkpoint/interrupt 是否能替代 Lumen 当前的请求边界澄清状态机。

运行：

```bash
uv run --with langgraph python scripts/spikes/langgraph_context_resume.py
```

验证链路为 `prepare → agent → interrupt → resume → commit`，并保持 `session_id == thread_id`。结果同时验证：resume 会从包含 `interrupt` 的 node 起点重新执行，而不是恢复 Python 调用栈中的精确行；因此 interrupt 前的副作用仍必须幂等。

结论：当前 P0/P1 不采用 LangGraph。Lumen 的澄清只需要跨模型请求边界恢复，现有 `SessionRepository + SessionContextState` 已是执行状态权威。只有需要分支/join、time travel、跨日节点级续跑或故障后 super-step 恢复，并且 LangGraph checkpointer 能替换现有执行状态而非形成双写时，才重新评估。

参考：[LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
