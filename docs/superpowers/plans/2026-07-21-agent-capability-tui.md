# Agent Capability and TUI Execution Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured planning, public progress, controlled workspace mutation and command execution, context compaction, and an execution-timeline TUI while retaining the Pydantic AI loop.

**Architecture:** A per-run `TaskController` exposes three side-effect-free control tools to the model and emits typed events. Workspace capability tools join the existing registry and permission path. A `ContextManager` compacts active history before a run while the session repository preserves full append-only records; Textual renders plan, progress, approvals, tool state, and final output as separate timeline elements.

**Tech Stack:** Python 3.11–3.13, Pydantic AI, Pydantic 2, Textual, Typer, pytest/pytest-asyncio, Ruff, Pyright, uv.

## Global Constraints

- Keep Pydantic AI; do not introduce LangGraph.
- Do not add subagents or a non-interactive CLI.
- Never expose provider reasoning fields or private chain-of-thought.
- `write_file`, `edit_file`, and `run_command` are workspace-scoped and approval-gated by default.
- `run_command` uses argv execution without shell interpolation and terminates its process group on timeout or cancellation.
- Preserve full JSONL history when active model context is compacted.
- Preserve stdio and Streamable HTTP MCP behavior and the existing permission order.
- Keep `pi/tui/` unchanged and outside Python runtime dependencies.
- The directory is intentionally not a Git repository; do not initialize Git or add commit steps during execution.

## File Map

- Create `src/lumen/plan.py`: strict plan models and state transitions.
- Create `src/lumen/task_control.py`: control-tool implementations bound to one run and its event sink.
- Create `src/lumen/context.py`: history estimation, structured summary model, compaction result.
- Create `src/lumen/tools/capability.py`: write, exact edit, argv command execution.
- Create `src/lumen/ui/plan_panel.py`: plan rendering.
- Create `src/lumen/ui/tool_card.py`: tool lifecycle and inline approval widget.
- Modify `src/lumen/config.py`: context configuration and new builtin names.
- Modify `src/lumen/events.py`: plan, progress, compaction, approval, timing, and classified-text events.
- Modify `src/lumen/runtime.py`: task controller, event metrics, text classification, context preparation.
- Modify `src/lumen/resources.py`: capability/control registration, model reuse, metadata and collisions.
- Modify `src/lumen/sessions.py`: schema v2 plan/diagnostic/compaction records with v1 loading.
- Modify `src/lumen/ui/app.py`: plan area, timeline, inline decisions, metrics, restored state.
- Remove `src/lumen/ui/approval.py` after all callers and tests use inline approval.
- Modify `agent.example.yaml` and `README.md`: configuration, safety, UI and examples.
- Add or modify focused tests under `tests/` for every behavior below.

---

### Task 1: Plan Models, Control Tools, and Configuration

**Files:**
- Create: `src/lumen/plan.py`
- Create: `src/lumen/task_control.py`
- Modify: `src/lumen/config.py`
- Modify: `src/lumen/events.py`
- Create: `tests/test_task_control.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `PlanStepInput`, `PlanStep`, `PlanState`, `StepStatus`.
- Produces: `TaskController.start(plan, emit)`, `set_plan`, `update_step`, `report_progress`, and `snapshot`.
- Produces events: `PlanCreated`, `PlanUpdated`, `ProgressReported`.
- Produces config: `ContextConfig(enabled, soft_token_limit, keep_recent_turns, summary_max_tokens)` at `AppConfig.context`.

- [ ] **Step 1: Write failing plan-transition tests**

```python
async def test_controller_creates_and_updates_one_active_step() -> None:
    events: list[RunEvent] = []
    controller = TaskController()
    controller.start(PlanState(), events.append)
    await controller.set_plan([
        PlanStepInput(id="inspect", title="Inspect project"),
        PlanStepInput(id="test", title="Run tests"),
    ])
    await controller.update_step("inspect", StepStatus.IN_PROGRESS, "Reading config")
    assert controller.snapshot().steps[0].status is StepStatus.IN_PROGRESS
    assert isinstance(events[0], PlanCreated)
    assert isinstance(events[1], PlanUpdated)


async def test_controller_rejects_completed_to_pending() -> None:
    controller = TaskController()
    controller.start(PlanState(), lambda _event: None)
    await controller.set_plan([PlanStepInput(id="one", title="One")])
    await controller.update_step("one", StepStatus.IN_PROGRESS)
    await controller.update_step("one", StepStatus.COMPLETED)
    with pytest.raises(ValueError, match="completed step"):
        await controller.update_step("one", StepStatus.PENDING)
```

- [ ] **Step 2: Run tests and verify the missing-module failure**

Run: `uv run pytest tests/test_task_control.py -v`

Expected: collection fails because `lumen.plan` and `lumen.task_control` do not exist.

- [ ] **Step 3: Implement strict plan state and transitions**

```python
class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class PlanStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)


class PlanStep(PlanStepInput):
    status: StepStatus = StepStatus.PENDING
    note: str | None = Field(default=None, max_length=500)


class PlanState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(default=0, ge=0)
    steps: list[PlanStep] = Field(default_factory=list)
```

`TaskController.update_step` must locate one stable ID, reject a second `in_progress` step, reject every transition out of `completed`, construct a new immutable snapshot, and emit the new snapshot. `set_plan` rejects duplicate IDs and increments `revision`.

- [ ] **Step 4: Add progress event behavior and test it**

```python
async def test_progress_is_public_and_bounded() -> None:
    events: list[RunEvent] = []
    controller = TaskController()
    controller.start(PlanState(), events.append)
    result = await controller.report_progress("Found the config entry.", "Inspect tests")
    assert result == "Progress reported."
    assert events == [ProgressReported("Found the config entry.", "Inspect tests")]
```

The controller rejects empty summaries and text over 800 characters. The system prompt, not a keyword filter, enforces that this is public rationale rather than hidden reasoning.

- [ ] **Step 5: Add context configuration tests and implementation**

```python
def load_yaml(tmp_path: Path, text: str) -> AppConfig:
    path = tmp_path / "agent.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_context_config_is_strict_and_validated(tmp_path: Path) -> None:
    config = load_yaml(tmp_path, """
version: 1
agent: {model: {id: test}}
context:
  enabled: true
  soft_token_limit: 20000
  keep_recent_turns: 4
  summary_max_tokens: 1500
""")
    assert config.context.soft_token_limit == 20_000
```

Implement defaults of `enabled=True`, `soft_token_limit=60_000`, `keep_recent_turns=6`, and `summary_max_tokens=2_000`, all positive with `keep_recent_turns >= 1`.

- [ ] **Step 6: Run focused and regression tests**

Run: `uv run pytest tests/test_task_control.py tests/test_config.py -v`

Expected: all tests pass. Record this as the Task 1 checkpoint; do not create a Git commit.

---

### Task 2: Workspace Write, Exact Edit, and Command Tools

**Files:**
- Create: `src/lumen/tools/capability.py`
- Modify: `src/lumen/tools/builtin.py`
- Modify: `src/lumen/config.py`
- Create: `tests/test_capability_tools.py`
- Modify: `tests/test_builtin_tools.py`

**Interfaces:**
- Produces: `build_capability_specs(root, *, max_timeout) -> list[ToolSpec]`.
- Tool names: `write_file`, `edit_file`, `run_command`.
- Reuses: `Workspace`, `WorkspaceViolation`, `Risk`, `ToolSpec`, and the 64 KiB output policy.

- [ ] **Step 1: Write failing write and edit tests**

```python
def capability(tmp_path: Path, name: str) -> Callable[..., Any]:
    specs = build_capability_specs(tmp_path, max_timeout=2.0)
    return next(spec.function for spec in specs if spec.name == name)


def test_write_requires_explicit_overwrite(tmp_path: Path) -> None:
    write_file = capability(tmp_path, "write_file")
    (tmp_path / "note.txt").write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        write_file("note.txt", "new")
    assert write_file("note.txt", "new", overwrite=True) == "Wrote 3 bytes to note.txt"


def test_edit_requires_exactly_one_match(tmp_path: Path) -> None:
    edit_file = capability(tmp_path, "edit_file")
    path = tmp_path / "code.py"
    path.write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="2 matches"):
        edit_file("code.py", "x = 1", "x = 2")
    assert path.read_text(encoding="utf-8") == "x = 1\nx = 1\n"
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_capability_tools.py -k 'write or edit' -v`

Expected: missing `lumen.tools.capability`.

- [ ] **Step 3: Implement atomic write and exact edit**

Resolve the destination and its parent through `Workspace`. Reject non-directory parents and symlink escapes. Write to a sibling temporary file, flush and fsync it, then use `os.replace` so cancellation cannot leave partial content. `edit_file` reads UTF-8, counts exact occurrences before any write, and delegates the final atomic replacement.

```python
return [
    ToolSpec(write_file, risk=Risk.WRITE),
    ToolSpec(edit_file, risk=Risk.WRITE),
    ToolSpec(run_command, risk=Risk.EXECUTE, timeout=max_timeout),
]
```

- [ ] **Step 4: Write failing command execution tests**

```python
async def test_run_command_captures_exit_code_without_shell(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    result = await run_command(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]
    )
    assert result["exit_code"] == 3
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"


async def test_run_command_rejects_cwd_escape(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    with pytest.raises(WorkspaceViolation):
        await run_command([sys.executable, "-V"], cwd="..")
```

- [ ] **Step 5: Implement process-group timeout, cancellation, and bounded output**

Use `asyncio.create_subprocess_exec(*argv, cwd=resolved_cwd, stdout=PIPE, stderr=PIPE, start_new_session=True)`. Clamp a requested timeout to `min(requested, max_timeout)`. On timeout or `CancelledError`, send `SIGTERM` to `os.getpgid(process.pid)`, wait briefly, then send `SIGKILL` if required. Always reap the child. Return a JSON-serializable dictionary with `argv`, relative `cwd`, `exit_code`, `stdout`, `stderr`, `elapsed_seconds`, `timed_out`, and per-stream truncation flags.

- [ ] **Step 6: Add builtin selection and risk tests**

```python
def test_capability_tools_are_opt_in_builtins(tmp_path: Path) -> None:
    config = load_yaml(tmp_path, """
version: 1
agent: {model: {id: test}}
tools: {builtins: [read_file, write_file, edit_file, run_command]}
""")
    assert config.tools.builtins[-3:] == ["write_file", "edit_file", "run_command"]
```

`ResourceManager` registration is deferred to Task 3. This step only extends the strict literal and changes `build_builtin_specs` to `build_builtin_specs(root: str | Path, *, max_timeout: float = 60.0) -> list[ToolSpec]`, returning the read specs followed by `build_capability_specs(root, max_timeout=max_timeout)`.

- [ ] **Step 7: Run focused and regression tests**

Run: `uv run pytest tests/test_capability_tools.py tests/test_builtin_tools.py tests/test_config.py -v`

Expected: all tests pass. Record the Task 2 checkpoint without Git.

---

### Task 3: Integrate Task Control, Capability Tools, and Rich Events into Runtime

**Files:**
- Modify: `src/lumen/events.py`
- Modify: `src/lumen/runtime.py`
- Modify: `src/lumen/resources.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_resources.py`
- Modify: `tests/test_tool_registry.py`

**Interfaces:**
- Extends: `AgentRuntime.run(prompt, history, emit, approve, *, plan=None) -> RunOutcome`.
- Extends: `RunOutcome.plan`, `active_history`, `diagnostics`, and timing-aware usage.
- Consumes: `TaskController`, control tools, and capability specs.

- [ ] **Step 1: Write a failing deterministic planning loop test**

Build a `FunctionModel` that first calls `set_plan`, then `report_progress`, then a read tool, then `update_step`, and finally returns text. Assert event order:

```python
assert [type(event) for event in events if isinstance(
    event, (PlanCreated, ProgressReported, ToolCallStarted, ToolCallFinished, PlanUpdated)
)] == [PlanCreated, ProgressReported, ToolCallStarted, ToolCallFinished, PlanUpdated]
assert outcome.plan.steps[0].status is StepStatus.COMPLETED
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_runtime.py::test_runtime_emits_structured_plan_and_progress -v`

Expected: `AgentRuntime.run` has no plan/controller behavior.

- [ ] **Step 3: Register control tools and per-run state**

Construct one `TaskController` per `AgentRuntime`, register its bound async methods as sequential tools, and add metadata with `origin="control"`, `risk="read"`, and `control="true"`. Before each run call `controller.start(plan or PlanState(), emit)`. Add exact control names to ResourceManager's collision set so a plugin or MCP server cannot shadow them.

Append to the base instructions:

```text
For work requiring three or more actions, any file mutation, command execution,
or several coordinated tools, call set_plan before acting. Use report_progress
only for concise public updates: findings, changes, errors, recovery, and next
action. Never place private reasoning or hidden chain-of-thought in progress.
Keep plan steps current and complete or block each step before the final answer.
```

- [ ] **Step 4: Extend tool and approval events with metadata and time**

`ToolCallStarted` carries `origin`, `risk`, and `started_at`. Store start times by call ID. `ToolCallFinished` carries `elapsed_seconds`, `preview`, `error_category`, and optional `exit_code`. Emit `ToolApprovalPending(request)` before the callback and `ToolApprovalResolved(call_id, approved, message)` after it.

Normalize exceptions into `ToolErrorInfo(category, message, retryable)` without including tracebacks or secrets in model-visible text.

- [ ] **Step 5: Add capability metadata and approval regression test**

Create a config with `write_file` and `run_command`, open `ResourceManager`, and assert both appear with `builtin` origin and `write`/`execute` risk. Drive a deferred `write_file` call and assert it cannot execute before approval and denial produces `ToolDenied`.

- [ ] **Step 6: Track MCP health for the top bar**

Add `ResourceManager.mcp_status: dict[str, str]`, initialized to `connecting` for configured servers. Set each entry to `ok` after discovery or `error` when an optional server degrades. Required failures still raise after recording `error`. Extend `summary()` with this mapping and test one healthy and one optional-failed server.

- [ ] **Step 7: Add run metrics**

Measure monotonic elapsed time. `UsageUpdated` includes Pydantic usage plus `request_count`, `tool_call_count`, `context_tokens_estimate`, and `elapsed_seconds`. Do not derive private reasoning token content; numeric provider usage fields may remain in usage data.

- [ ] **Step 8: Run focused and regression tests**

Run: `uv run pytest tests/test_runtime.py tests/test_resources.py tests/test_tool_registry.py -v`

Expected: all tests pass, including existing MCP-compatible tool event tests. Record the Task 3 checkpoint without Git.

---

### Task 4: Context Compaction and Session Schema v2

**Files:**
- Create: `src/lumen/context.py`
- Modify: `src/lumen/runtime.py`
- Modify: `src/lumen/resources.py`
- Modify: `src/lumen/sessions.py`
- Create: `tests/test_context.py`
- Modify: `tests/test_sessions.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Produces: `ContextSummary`, `CompactionRecord`, `PreparedContext`, `ContextManager.prepare`.
- Extends: `SessionRepository.append_turn(session_id, *, user_input, messages, approvals, usage, status, plan, diagnostics, compaction)`.
- Extends: `SessionData.plan`, `history` as active history, and `full_history` as every completed raw message.

- [ ] **Step 1: Write failing estimation and retention tests**

```python
def test_estimate_tokens_is_deterministic() -> None:
    messages = [ModelRequest(parts=[UserPromptPart(content="x" * 400)])]
    assert estimate_message_tokens(messages) >= 100


def test_recent_turns_keeps_complete_user_boundaries() -> None:
    history: list[ModelMessage] = []
    for number in range(3):
        history.extend([
            ModelRequest(parts=[UserPromptPart(content=f"question {number}")]),
            ModelResponse(parts=[TextPart(content=f"answer {number}")]),
        ])
    kept = retain_recent_turns(history, 2)
    assert sum(
        isinstance(part, UserPromptPart)
        for message in kept if isinstance(message, ModelRequest)
        for part in message.parts
    ) == 2
```

Use serialized UTF-8 length divided by four, rounded up, as the documented provider-independent estimate.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_context.py -v`

Expected: missing `lumen.context`.

- [ ] **Step 3: Implement structured summary and prepared context**

```python
class ContextSummary(BaseModel):
    goals: list[str]
    constraints: list[str]
    completed: list[str]
    current_plan: list[str]
    important_files: list[str]
    key_facts: list[str]
    failures_and_approvals: list[str]
    outstanding: list[str]


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    summary: ContextSummary
    active_history: list[ModelMessage]
    source_message_count: int
    usage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedContext:
    history: list[ModelMessage]
    compaction: CompactionRecord | None
    usage: dict[str, Any]
```

`ContextManager.prepare` emits compaction start/completed/failed events. It creates a tool-free `Agent(model, output_type=ContextSummary)` with `UsageLimits(request_limit=1, total_tokens_limit=summary_max_tokens)`. A successful result becomes one `ModelRequest` containing a `SystemPromptPart` labeled as a prior-conversation summary, followed by recent complete turns.

- [ ] **Step 4: Integrate preparation into runtime**

At the beginning of `run`, call `prepare(history, plan, diagnostics, emit)`. Pass prepared history to Pydantic AI. Return `active_history = prepared.history + result.new_messages()`. Merge compaction numeric usage into final aggregate usage while keeping the task's request/tool `UsageLimits` unchanged.

- [ ] **Step 5: Write failing session v2 round-trip and v1 compatibility tests**

```python
def test_session_restores_latest_compacted_active_history(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="test")
    old_messages = [ModelRequest(parts=[UserPromptPart(content="old question")])]
    compacted = [ModelRequest(parts=[SystemPromptPart(content="Prior summary")])]
    new_messages = [
        ModelRequest(parts=[UserPromptPart(content="new question")]),
        ModelResponse(parts=[TextPart(content="new answer")]),
    ]
    plan = PlanState(steps=[PlanStep(id="one", title="One", status=StepStatus.COMPLETED)])
    summary = ContextSummary(
        goals=["finish work"], constraints=[], completed=["one"], current_plan=["one: completed"],
        important_files=[], key_facts=[], failures_and_approvals=[], outstanding=[]
    )
    record = CompactionRecord(summary, compacted, len(old_messages), {})
    repo.append_turn(
        session.id,
        user_input="old question",
        messages=old_messages,
        approvals=[],
        usage={},
        status="completed",
        plan=PlanState(),
        diagnostics=[],
        compaction=None,
    )
    repo.append_turn(
        session.id,
        user_input="new question",
        messages=new_messages,
        approvals=[],
        usage={},
        status="completed",
        plan=plan,
        diagnostics=[],
        compaction=record,
    )
    loaded = repo.load(session.id)
    assert loaded.plan == plan
    assert loaded.history == record.active_history + new_messages
    assert loaded.full_history == old_messages + new_messages


def test_schema_one_session_loads_with_empty_plan(tmp_path: Path) -> None:
    session_id = "00000000-0000-0000-0000-000000000001"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({
            "type": "session", "schema_version": 1, "id": session_id,
            "agent_name": "legacy", "model_id": "test", "created_at": "2026-01-01T00:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )
    loaded = SessionRepository(tmp_path).load(session_id)
    assert loaded.plan == PlanState()
```

- [ ] **Step 6: Upgrade JSONL without losing append-only history**

New session headers use schema version 2. Turn records add `plan`, `diagnostics`, and optional `compaction`. Loaders accept version 1 or 2. For v1, missing fields receive empty defaults. `full_history` always extends completed raw turn messages. On a compaction record, reset `active_history` to the stored compacted prefix, then append that turn's new messages and later successful turns.

- [ ] **Step 7: Test failure fallback**

Use a failing summary `FunctionModel`; assert `ContextCompactionFailed` is emitted, the main model receives the original history, and the run completes. Assert no compaction record is persisted.

- [ ] **Step 8: Run focused and regression tests**

Run: `uv run pytest tests/test_context.py tests/test_sessions.py tests/test_runtime.py -v`

Expected: all tests pass. Record the Task 4 checkpoint without Git.

---

### Task 5: Classify Intermediate Commentary and Final Text

**Files:**
- Modify: `src/lumen/events.py`
- Modify: `src/lumen/runtime.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Produces: `CommentaryDelta(text)` for text in a response that also calls tools.
- Retains: `TextDelta(text)` exclusively for the final response.

- [ ] **Step 1: Write failing mixed-response classification test**

Create a streaming `FunctionModel` response that yields `"Inspecting configuration."` and a tool call in the same model response, followed by a final text-only response. Assert:

```python
commentary = "".join(event.text for event in events if isinstance(event, CommentaryDelta))
final = "".join(event.text for event in events if isinstance(event, TextDelta))
assert commentary == "Inspecting configuration."
assert final == "Configuration is valid."
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_runtime.py::test_runtime_separates_commentary_from_final_text -v`

Expected: all text is currently emitted as `TextDelta`.

- [ ] **Step 3: Implement response-scoped buffering**

Buffer text parts for the current model response. If the response produces any `FunctionToolCallEvent`, flush buffered and subsequent response text as `CommentaryDelta`. If it produces the final `AgentRunResultEvent` without a tool call, flush as `TextDelta`. Clear the response buffer at the Pydantic response boundary. Do not inspect model-specific reasoning fields.

- [ ] **Step 4: Preserve final result correctness**

Assert `RunOutcome.output` and `RunCompleted.output` equal only the terminal model result, even when intermediate commentary exists. Assert a tool-only response emits no commentary event.

- [ ] **Step 5: Run runtime and MCP tests**

Run: `uv run pytest tests/test_runtime.py tests/test_mcp_integration.py -v`

Expected: all tests pass. Record the Task 5 checkpoint without Git.

---

### Task 6: Plan Panel, Timeline Tool Cards, and Inline Approval

**Files:**
- Create: `src/lumen/ui/plan_panel.py`
- Create: `src/lumen/ui/tool_card.py`
- Modify: `src/lumen/ui/app.py`
- Delete: `src/lumen/ui/approval.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Produces: `PlanPanel.update_plan(plan)`.
- Produces: `ToolCard.start/update_result/set_approval_pending/resolve_approval`.
- Produces Textual message: `ToolCard.Decision(call_id, approved)`.
- `LumenApp` owns `dict[str, Future[ToolApproval]]` approval waiters.

- [ ] **Step 1: Write failing PlanPanel Pilot test**

```python
async def test_plan_panel_marks_active_and_completed_steps() -> None:
    app = PlanHost(PlanState(steps=[
        PlanStep(id="one", title="Inspect", status=StepStatus.COMPLETED),
        PlanStep(id="two", title="Test", status=StepStatus.IN_PROGRESS),
    ]))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.query_one(PlanPanel).render())
    assert "✓ Inspect" in text
    assert "● Test" in text
```

- [ ] **Step 2: Implement responsive PlanPanel**

Render `✓`, `●`, `○`, and `!` for completed, active, pending, and blocked. Apply CSS classes for each state. The panel is collapsible below 100 columns and expanded otherwise; use Textual resize events rather than terminal escape sequences.

- [ ] **Step 3: Write failing tool-card lifecycle and decision tests**

```python
async def test_tool_card_resolves_inline_denial() -> None:
    async with ToolCardHost().run_test() as pilot:
        card = app.query_one(ToolCard)
        card.set_approval_pending(request)
        await pilot.click(card.query_one(".deny", Button))
        decision = await app.next_decision()
    assert decision == (request.call_id, False)
```

- [ ] **Step 4: Implement timeline cards**

Each card header includes icon/status, name, risk, origin, and elapsed time. Its collapsed preview shows arguments while running and the result preview when finished. The expanded body keeps JSON arguments and complete bounded result/error. Pending cards mount `Allow once` and `Deny` buttons; a decision disables both immediately and emits exactly one message.

- [ ] **Step 5: Replace modal approval with futures tied to cards**

On `ToolApprovalPending`, update or create the card and allocate a future by call ID. The runtime approval callback awaits that future. `ToolCard.Decision` resolves it with `ToolApproval`; `ToolApprovalResolved` updates visual state. Cancellation resolves every pending future as denied before cancelling the worker. Remove `ApprovalModal` and its tests only after the inline test passes.

- [ ] **Step 6: Render progress, classified text, compaction, and metrics**

- `PlanCreated` and `PlanUpdated` update `PlanPanel`.
- `ProgressReported` and `CommentaryDelta` render subdued analysis-summary blocks.
- `TextDelta` updates only the final Markdown widget.
- Compaction events render one mutable status row rather than three messages.
- Tool events update one card per call ID.
- Topbar displays MCP healthy/configured and context percentage.
- Status displays request count, tool count, tokens, and elapsed time.

- [ ] **Step 7: Restore visible plan and active context**

`_load_session` assigns `loaded.plan` and `loaded.history`, updates the plan panel after mount, and renders a concise session-restored notice. It does not replay every historical tool card. `/new` resets plan and history. `/retry` starts from the last valid active history and latest diagnostic plan.

- [ ] **Step 8: Add narrow-screen and cancellation Pilot tests**

Resize to 80 columns and assert the plan is collapsible. Start an approval wait, invoke `action_cancel_run`, and assert no pending future remains. Ensure final answer Markdown never contains progress summary text.

- [ ] **Step 9: Run focused TUI tests**

Run: `uv run pytest tests/test_tui.py -v`

Expected: all tests pass without hanging workers or pending asyncio tasks. Record the Task 6 checkpoint without Git.

---

### Task 7: End-to-End Scenario, Documentation, and Full Verification

**Files:**
- Create: `tests/test_balanced_agent_integration.py`
- Modify: `agent.example.yaml`
- Modify: `README.md`
- Modify any existing tests whose event constructor signatures changed.

**Interfaces:**
- Validates the public feature contract rather than adding a new runtime interface.

- [ ] **Step 1: Write the end-to-end deterministic scenario**

Use a temporary workspace and `FunctionModel` to drive:

```text
set_plan
report_progress
read_file
edit_file (approved)
run_command (approved)
update_step completed
final Markdown
```

Assert the file changed exactly once, command exit code is zero, events occur in semantic order, final output excludes commentary, and `SessionRepository.load` restores completed plan plus active history.

- [ ] **Step 2: Add denial and recovery scenario**

Drive `run_command`, deny it, have the model report a public fallback and use `read_file`, then finish. Assert the process never starts, `ToolApprovalResolved.approved` is false, plan finishes in a valid state, and the final response states the alternative result.

- [ ] **Step 3: Update example configuration**

Add:

```yaml
context:
  enabled: true
  soft_token_limit: 60000
  keep_recent_turns: 6
  summary_max_tokens: 2000

tools:
  builtins:
    - read_file
    - list_directory
    - search_text
    - write_file
    - edit_file
    - run_command
```

Keep write and execute tools out of `always_allow`, so the example demonstrates approval.

- [ ] **Step 4: Update README**

Document public progress versus private reasoning, plan behavior, exact write/edit guarantees, argv command examples, no-shell semantics, process permissions, compaction preservation, inline approval, timeline symbols, and restored sessions. Explicitly state that command approval is not a sandbox.

- [ ] **Step 5: Run format and static checks**

Run:

```bash
uv run ruff format .
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Expected: Ruff passes, all files are formatted, and Pyright reports 0 errors and 0 warnings.

- [ ] **Step 6: Run the complete test suite**

Run: `uv run pytest`

Expected: every unit, deterministic runtime, Textual Pilot, stdio MCP, and Streamable HTTP MCP test passes.

- [ ] **Step 7: Validate lock, package, and example discovery**

Run:

```bash
uv lock --check
uv build
OPENAI_API_KEY=test-only uv run lumen --config agent.example.yaml --cwd . --check-config
```

Expected: lock is current; wheel and sdist build; config check lists control tools plus selected local/plugin/MCP tools with no collision; no real model request is made.

- [ ] **Step 8: Review scope and preserved assets**

Use `rg --files src tests docs agent.example.yaml README.md` to inspect the deliverables. Verify `pi/tui/` was not changed, no Git repository was initialized, no non-interactive CLI or subagent was added, and the final documentation contains no claim that private chain-of-thought is exposed.
