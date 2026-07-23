# Agent Capability and TUI Execution Timeline Design

Date: 2026-07-21 (initial) · 2026-07-22 (revised to match shipped implementation + robustness fixes)
Status: Implemented

> **Document status.** This spec was originally written before implementation
> began. It has been revised after the fact to describe what actually shipped,
> so it can serve as the authoritative reference for the current codebase. The
> companion file
> `docs/superpowers/plans/2026-07-21-agent-capability-tui.md` records the
> step-by-step plan history and is left unchanged as a historical artifact.
>
> Sections marked **[Revised]** changed materially between the original design
> and the shipped implementation; the rationale is noted inline.

## 1. Goal and Scope

Evolve `lumen` from a minimal single-agent MCP loop into a balanced
general-purpose agent. The result must remain useful for business MCP
workflows while gaining enough controlled coding capability for multi-step
work.

This change adds:

- Structured plans and task progress.
- Public action summaries without exposing private chain-of-thought.
- Built-in workspace-scoped write, edit, and command tools.
- Automatic conversation context compaction.
- A Textual execution timeline that clearly separates plans, progress, tools,
  and final answers.

This change does not add subagents, LangGraph, a non-interactive CLI, an
operating-system sandbox, session branching, or a web UI.

## 2. Architecture

Pydantic AI remains the model and tool-loop runtime. A task-control layer is
added around it instead of replacing the loop with LangGraph.

```text
User request
    |
    v
TaskController
    |- PlanState: steps, statuses, public progress, failure notes
    |- ContextManager: context estimation, token-budgeted compaction
    `- ControlTools: set_plan, update_step, report_progress
    |
    v
Pydantic AI Agent Loop
    |- Read tools: read_file, list_directory, search_text
    |- Write tools: write_file, edit_file
    |- Execute tool: run_command
    `- Plugin and MCP tools
    |
    v
Stable RunEvent stream
    |
    v
Textual TUI
    |- Fixed PlanPanel (pinned at top)
    |- Scrolling message timeline (Lazy-rendered)
    `- Approval selector (pi-style, keyboard-driven)
```

Simple requests do not require a plan. The system instructions require
`set_plan` before work expected to need at least three actions, any workspace
mutation, command execution, or coordination across multiple tools.

Control tools modify only task state and session records, so they never
require approval. Side-effecting tools remain sequential. The effective plan
and public progress are persisted after a turn. Failed and cancelled turns do
not enter valid model history, but their plan snapshot, approvals, and
diagnostics remain available for retry and inspection.

### 2.1 Multi-model support [Added after initial design]

`AgentSection` accepts either a single `model:` (legacy) or a `models:` map
plus an optional `default_model:` (mutually exclusive). The active model can
be switched at runtime via `/model <name>` or the command palette without
reconnecting MCP servers — `ResourceManager` uses a two-stack `AsyncExitStack`
so MCP clients persist while the runtime stack (model + tools) rebuilds. See
`src/lumen/resources.py` and `src/lumen/models.py`.

## 3. Task Control

### 3.1 Plan model

A plan contains ordered steps with stable identifiers. Each step has:

- `id`
- `title`
- `status`: `pending`, `in_progress`, `completed`, or `blocked`
- Optional public `note`

At most one step may be `in_progress`. Completed steps cannot return to
`pending`. Replacing the whole plan is permitted only through `set_plan` and
is recorded as a new plan revision. Invalid transitions are returned to the
model as control-tool errors.

### 3.2 Control tools

- `set_plan(steps)` creates or replaces the current plan.
- `update_step(step_id, status, note=None)` performs a validated transition.
- `report_progress(summary, next_action=None)` emits concise information
  intended for the user.

The system prompt explicitly prohibits submitting hidden reasoning, private
scratch work, or chain-of-thought through these tools. Progress should state
what was learned, what changed, and what will happen next.

## 4. Built-in Capability Tools

### 4.1 `write_file`

Signature: `write_file(path, content, overwrite=False)`.

The path must resolve inside the workspace. Existing files are protected
unless `overwrite` is explicitly true. The tool has `write` risk and requires
approval unless covered by `always_allow` or auto-mode short-circuit (see §5).

### 4.2 `edit_file`

Signature: `edit_file(path, old_text, new_text)`.

The tool performs one exact replacement. `old_text` must occur exactly once;
zero or multiple matches fail without modifying the file. The path follows
the same traversal and symlink protections as existing file tools. The tool
has `write` risk.

### 4.3 `run_command`

Signature: `run_command(argv, cwd=".", timeout_seconds=None)`.

`argv` is a non-empty list of arguments and is executed without shell
interpolation. The working directory must resolve inside the workspace. A
call may request a shorter timeout but cannot exceed the configured tool
timeout. Output records bounded stdout, bounded stderr, exit code, elapsed
time, timeout state, and truncation. A timeout or cancellation terminates the
process group to prevent orphaned children. The tool has `execute` risk.

These controls reduce accidental actions but are not an operating-system
sandbox. Plugins, MCP servers, and approved commands retain the current
user's process permissions.

## 5. Permission and Approval Behavior [Revised]

Permission evaluation remains:

1. `always_deny` tools are hidden from the model.
2. `always_allow` tools execute automatically.
3. `read` tools execute automatically.
4. Other tools require approval (modified by approval mode, below).
5. Rejection returns `ToolDenied` to the model.

### 5.1 Approval modes: `manual`, `accept_edits`, `auto` [Revised]

`PermissionsConfig.default_mode` controls the session-start mode, toggleable
live via `/mode`, `Ctrl+M`, `Shift+Tab`, or the command palette — no runtime rebuild
needed.

- **`manual`** (default; legacy `ask` maps here) — every tool the permission policy routes to `CONFIRM`
  mounts the approval selector.
- **`accept_edits`** — builtin `write_file` and `edit_file` requests are
  approved; commands, plugin operations, and MCP writes still prompt.
- **`auto`** — every explicitly classified risk (`read`, `write`, `execute`,
  and legacy `external`) is auto-approved without mounting a selector.
  `external_unknown` still prompts because it represents a remote capability
  the operator has not classified.

The short-circuit is decided by `ApprovalPolicy` and consumed by
`LumenApp._await_inline_approval`, the single choke point through which
both local and MCP approvals flow. The runtime no longer emits
`ToolApprovalPending` itself — the `approve` callback owns that surface so
auto-mode can skip the card entirely instead of mounting-then-resolving.

### 5.2 Inline approval selector [Revised — no more buttons]

The original design specified "Allow once / Deny" controls inside each tool
card. The shipped implementation uses one queue-aware `ApprovalPanel` fixed
directly above the composer, while tool cards remain compact audit records:

- The panel shows `Allow once` and `Deny` as vertical options and starts with
  no selection.
- Navigation is intentionally limited to `↑`/`↓` plus `Enter`; Y/N shortcuts
  are not accepted. `Esc` cancels the whole run.
- Consecutive approval requests stay in one stable queue and advance in place.
- On decision, focus returns to the prompt editor automatically.

The card displays origin, risk, complete arguments, elapsed time, and result
preview. A denied call includes the denial message in the model-visible
result so the agent can revise its plan and choose an alternative.

## 6. Context Management [Revised]

Configuration gains a strict `context` section:

- `enabled` (default `true`)
- `soft_token_limit` (default `60000`) — estimated-token trigger threshold
- `keep_recent_tokens` (default `20000`) — **token budget** for the retained
  window (replaces the older `keep_recent_turns` count-based cut)
- `summary_tool_result_chars` (default `2000`) — per-tool-result character cap
  when serializing history for the summarizer
- `summary_max_tokens` (default `2000`) — summarizer generation budget

### 6.1 What was removed

- **`keep_recent_turns`** — the field is gone entirely. A fixed turn count
  was unpredictable (6 turns could be 2K or 60K tokens depending on size).
  Replaced by the token-budgeted cut (§6.3). `StrictModel(extra="forbid")`
  rejects any YAML that still sets it.
- **`LimitsConfig.total_tokens`** — the cumulative-token hard wall is gone
  entirely. coding-agent doesn't set one either; context growth is handled by
  the compaction system (this section) rather than by halting the run. Only
  `request_count` and `tool_calls` remain as anti-runaway guards.

### 6.2 Estimation and trigger

Before a user run, `ContextManager.prepare` estimates the active history
size via `estimate_message_tokens` (UTF-8 bytes / 4, rounded up). Below the
soft limit, no additional request is made. At or above the limit, it invokes
the configured model once without tools and requests a structured
`ContextSummary`:

- `goals`, `constraints`, `completed`, `current_plan`, `important_files`,
  `key_facts`, `failures_and_approvals`, `outstanding`

The active context becomes the compaction summary (as a `SystemPromptPart`)
plus the token-budgeted recent window (§6.3). The JSONL session retains all
original messages. A schema-versioned compaction record stores the summary
and retained-history boundary, allowing resume without repeating the
compaction request.

### 6.3 Token-budgeted cut point [New — mirrors coding-agent's `findCutPoint`]

`retain_recent_tokens(messages, keep_tokens)` walks backwards from the newest
message, accumulating token estimates until `keep_recent_tokens` is spent,
then snaps **forward** to the next safe boundary (start of a user-prompt
`ModelRequest`). This guarantees:

- **Predictable post-compaction size** regardless of turn granularity — a
  20K-token budget always retains ~20K tokens, whether that's 2 huge turns or
  20 tiny ones.
- **Tool-call/result integrity** — the cut never lands between a `ToolCallPart`
  and its `ToolReturnPart`, which the Anthropic/OpenAI APIs would reject as an
  orphaned tool result.

If the very first message already exceeds the budget, it's kept anyway —
dropping the user's original request would be worse than a slightly oversized
window.

### 6.4 Per-tool-result truncation at summary time [New]

`_serialize_for_summary` truncates each `ToolReturnPart` independently to
`summary_tool_result_chars` before joining, so one giant output (e.g. a 50KB
file read) can't crowd out the rest. The older global `[: N*4]` prefix slice
is gone — it silently dropped the most recent (most relevant) turns once the
budget filled. Each truncation appends a `[... N more chars truncated]`
notice. Mirrors coding-agent's `TOOL_RESULT_MAX_CHARS = 2000`.

`ToolReturnPart` and `ToolCallPart` are checked **before** the generic
`content` string branch, because they also expose a `content`/`args`
attribute that would otherwise match the string path and bypass truncation.

### 6.5 Iterative summary update [New]

When a prior `ContextSummary` exists (passed via `previous_summary=` to
`ContextManager.prepare`), the summarizer receives it wrapped in
`<previous-summary>…</previous-summary>` and is instructed to PRESERVE
existing entries unless contradicted, ADD new information, and MOVE completed
items. This prevents the drift that from-scratch re-summarization causes on
long sessions that compact multiple times. Mirrors coding-agent's
`UPDATE_SUMMARIZATION_PROMPT`.

### 6.6 Failure handling

Compaction usage is recorded in diagnostics and aggregate token usage, but
does not consume the task's tool-call budget. If compaction fails, the
original history remains active, the TUI shows a warning, and the user run
continues. A provider rejection caused by excessive context may still fail
the run normally.

The current plan, unresolved tool failures, rejected approvals, and
unfinished steps are always included in the summary.

## 7. Event Contract

Stable events emitted by the runtime:

- `RunStarted`, `RunCompleted`, `RunFailed`, `RunCancelled`
- `TextDelta` (final assistant Markdown), `CommentaryDelta` (mid-run analysis)
- `PlanCreated`, `PlanUpdated`, `ProgressReported`
- `ToolCallStarted`, `ToolCallFinished`
- `ToolApprovalPending`, `ToolApprovalResolved`
- `ContextCompactionStarted`, `ContextCompactionCompleted`,
  `ContextCompactionFailed`
- `UsageUpdated`

Tool events carry origin, risk, timestamps, elapsed time, result preview, and
exit code where applicable. Usage events carry request count, tool count,
context estimate, and run elapsed time.

**Response-scoped text classification [Revised]:** Free-form text in a model
response is buffered until that response is classified. Text from a response
that also requests tools is emitted as `CommentaryDelta` (public action
commentary). Text from the response that ends the agent loop is emitted as
`TextDelta` (final Markdown). Provider reasoning tokens, hidden reasoning
fields, and private chain-of-thought are never read or rendered.

**Approval event ownership [Revised]:**
`ToolApprovalPending` is emitted by the `approve` callback (the UI), not by
the runtime. The runtime only emits `ToolApprovalResolved`. This lets
auto-mode skip the pending card entirely rather than mounting it and then
resolving it.

## 8. TUI Design [Revised]

The interface uses a **fixed plan panel pinned at the top** and a **scrolling
execution timeline below it**, so the plan stays visible while tool output
scrolls beneath it — the opencode/cursor layout:

```text
+ lumen · model glm-5.2 · mode auto · session · MCP 3/3 · ctx 42% +
| Plan                                                          [pinned] |
| [✓] step1: Inspect project                                              |
| [●] step2: Update configuration  ← in progress                         |
| [○] step3: Run tests                                                   |
+------------------------------------------------------------------------+
| User: Add configuration discovery                                      |
|                                                                        |
| [analysis summary]                                                     |
| Located the configuration entry point. Inspecting tests.               |
|                                                                        |
| ● read_file  (read · builtin · 0.02s)                                  |
| ● edit_file  (write · builtin)  → Allow    Deny   ← approval selector  |
|                                                                        |
| Assistant: Completed...                                                |
+------------------------------------------------------------------------+
| > _                                                                    |
+ Ready  │  auto · glm-5.2 · ctx 42% -----------------------------------+
```

### 8.1 Layout regions

From top to bottom, all **fixed** except the message timeline:

1. **Topbar** (1 row) — agent name, model, approval mode, session id, MCP
   health, context %. Mode always shows manual, accept_edits, or auto.
2. **PlanPanel** (pinned, `max-height: 8`) — the structured plan. Steps
   exceed 8 when the panel scrolls internally rather than pushing the
   timeline down. Hidden (`display: none`) until a plan arrives. Collapses to
   a one-line summary on terminals shorter than 20 rows.
3. **Message timeline** (`height: 1fr`, independently scrolling) — user
   messages, commentary, progress, tool cards, assistant Markdown.
4. **PromptEditor** (single rounded box, no Send button) — `Enter` submits,
   `Shift+Enter` inserts a newline. Focus ring is the only affordance.
5. **Status bar** (1 row) — left: run state (`Thinking…`, token summary);
   right: `│ <mode> · <model> · ctx <%>` suffix that persists across state
   changes.

The Textual `Footer` widget is deliberately omitted — it duplicated the
status bar's keymap hint in the bottom-right corner.

### 8.2 Theme system [New]

`src/lumen/ui/themes.py` registers two `textual.theme.Theme` instances
via `App.register_theme` in `on_mount`:

- **`lumen-dark`** (default) — soft blue-gray (`#0E1116` bg, `#7C8FF7` primary,
  `#4CC9F0` accent). Low-saturation opencode-inspired palette, calibrated for
  long sessions.
- **`lumen-light`** — GitHub-light neutral palette for bright environments.

All CSS uses design tokens (`$surface`, `$accent`, etc.) so both themes
resolve correctly. The scrollbar is tinted with the primary color and capped
to 1 cell width (posting pattern).

### 8.3 Streaming Markdown throttling [New]

Each `TextDelta` appends to a `StreamingMarkdownController`. A fixed 33ms
frame timer coalesces tokens without debounce starvation, and only one
Markdown update may execute at a time. `RunCompleted` / `RunFailed` /
`RunCancelled` force an awaited final flush; unmount closes the controller.
Without this, long streamed answers re-parse the entire markdown document on
every token or remain invisible until a continuous stream ends.

### 8.4 Bounded Timeline rendering [New]

`TimelineStore` owns structured items independently of Textual widgets and
exposes a 200-item view plus 20-Turn repository pages. User, system,
commentary, and progress messages still use `Lazy(Static(…))`, but the widget
tree itself is now bounded; pending approvals are retained beyond the cap.
Scrolling to the top loads an older page while preserving the visual anchor,
and leaving the bottom disables auto-follow until End is pressed. Assistant
Markdown and ToolCards are **not** lazy-wrapped — they're mutated during the
run, and Lazy would interfere with that.

### 8.5 `@path` file completion [New]

Typing `@` in the prompt editor triggers a tree-wide file search
(`src/lumen/ui/file_search.py`), ported from pi/tui's `autocomplete.ts`:

- **fd-first** — if `fd` is on PATH, shell out with `--base-directory .`
  (relative, so `.gitignore` is respected), `--max-results 100`, `--type f
  --type d`, no `--hidden` (it leaks `.venv`). Explicit `--exclude` for
  `.git`, `__pycache__`, `*.pyc`. Falls back to streaming `os.walk` with
  in-place `dirs[:]` pruning when `fd` is absent.
- **Two-stage cap** — collect 100 raw matches, score, return top 20.
- **Scoring** (pi's `scoreEntry`): exact filename = 100, startswith = 80,
  contains = 50, path-contains = 30, +10 for directories. Dots are literal
  (`@agent.md` only matches filenames containing the literal `agent.md`).
- **Scoped mode** — `@src/components/` restricts to that directory.
- **Best-match highlight** — `@rea` highlights `README.md`, not the
  alphabetically-first match.
- **Floating dropdown** — `CompletionDropdown` is `dock: top` + `layer:
  above` + dynamic `offset` from `anchor_above(target)`, so it floats above
  the prompt editor and never overlaps it.
- **Content expansion** — on submit, `@path` is expanded to
  `<file path="…">content</file>` blocks via `expand_file_mentions`, saving a
  `read_file` round-trip.

### 8.6 Command palette [New]

`Ctrl+P` opens Textual's native command palette, populated by
`LumenCommandProvider` (`src/lumen/ui/commands.py`). Commands are
built live from app state (model switching reflects configured models;
approval mode has `Switch to manual/accept-edits/auto` entries). Slash commands (`/help`,
`/model`, `/mode`, `/sessions`, `/resume`, `/tools`, `/retry`, `/quit`) are
the keyboard equivalent and share the same `action_*` methods.

## 9. Error and Cancellation Semantics

- Invalid control-tool calls return structured, model-visible errors.
- Tool failures surface a concise message; the model can revise its plan and
  report its recovery action after failure.
- A rejected tool preserves `ToolDenied` semantics.
- Command timeout or cancellation terminates the entire process group.
- Required MCP startup failure prevents a run; optional MCP failure is
  visible as degraded status.
- Failed and cancelled user turns do not mutate valid model history.
- Diagnostic, approval, plan, and compaction records remain append-only and
  fsynced.
- `IncompleteToolCall` (truncated mid-tool-call by `max_tokens`) surfaces a
  friendly hint pointing at the model's `settings.max_tokens`, not a generic
  failure.
- `UsageLimitExceeded` (only `request_count` / `tool_calls` can trip it now —
  `total_tokens` is removed) surfaces a friendly message naming the breached
  cap and the `agent.yaml` key to raise.

## 10. Configuration Schema

All sections use `StrictModel(extra="forbid")` — unknown fields are rejected
rather than silently ignored, which catches stale config after a rename (as
happened with `keep_recent_turns` → `keep_recent_tokens`).

```yaml
version: 1
agent:
  name: lumen
  instructions_file: null             # optional path to a system prompt
  model: {…}                          # legacy single-model form
  # OR
  models: {alpha: {…}, beta: {…}}     # multi-model form (mutually exclusive with model:)
  default_model: alpha                # optional; first key if unset
  limits:
    request_count: 50                 # per-run LLM request cap
    tool_calls: 100                   # per-run tool-call cap
    tool_timeout_seconds: 60
    # No total_tokens field — removed; context growth is handled by compaction
permissions:
  always_allow: []
  always_deny: []
  default_mode: manual                # manual | accept_edits | auto
tools:
  builtins: [read_file, list_directory, search_text]
  capabilities: [write_file, edit_file, run_command]
  plugins: []
mcp_servers:
  tyc-mcp:
    transport: stdio
    command: npx
    args: [-y, @tyc/mcp-server]
    tool_risks:
      search_companies_by_name: read   # others default to external_unknown
    required: true
context:
  enabled: true
  soft_token_limit: 60000
  keep_recent_tokens: 20000           # token budget for the retained window
  summary_tool_result_chars: 2000     # per-tool-result cap when summarizing
  summary_max_tokens: 2000
sessions:
  directory: .lumen/sessions
```

`ModelSettingsConfig` fields: `id`, `api_key` / `api_key_env`, `base_url`,
`api` (`chat` | `responses` | aliases), `settings` (max_tokens etc.).

## 11. Testing and Acceptance

Unit tests cover plan validation and transitions, control tools, write
overwrite protection, exact edit matching, command path containment,
timeout, cancellation, truncation, compaction thresholds and reconstruction,
the token-budgeted cut point, per-tool-result truncation, iterative summary
prompt construction, events, and the upgraded JSONL schema.

Deterministic `FunctionModel` scenarios cover:

- Plan, progress, read, approved edit, command, completed plan, final response.
- Tool failure followed by a revised plan and recovery.
- Command denial followed by a non-command alternative.
- Context compaction followed by a successful continuation.
- Approval selector keyboard navigation (no Button widgets).
- Auto-mode short-circuit for `read` / `external` risks.
- `UsageLimitExceeded` friendly message.

Textual Pilot tests cover plan rendering (pinned panel, not inside the
scrolling timeline), separation of progress from final output, tool-card
selector states, inline approval via keyboard, cancellation, narrow layouts,
restoration of plans and compacted context, dropdown positioning (never
overlaps the prompt), tree-wide `@` search excluding `.venv` / `__pycache__`,
slash-trigger symmetry (`/` works at any token boundary, not just col 0),
the completion race fix (synchronous trigger detection in `on_key`),
context-sensitive Esc (dropdown → cancel → clear), Ctrl+C cancel-or-quit,
Ctrl+Up/Down history navigation, and synchronous dropdown close on accept.

Skill unit tests cover frontmatter parsing, name validation and defaults,
missing-description drop, directory discovery (project + user), project-
overrides-user precedence, standalone `.md` file loading, `.git` /
`__pycache__` pruning, system-prompt XML formatting (structure, exclusion of
disabled skills, XML escaping), and manual-invocation expansion.

Acceptance commands:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv build
```

The existing stdio and Streamable HTTP MCP integration tests remain
mandatory.

## 12. Agent Skills [Added 2026-07-22]

lumen implements an Agent Skills system following the open
[Agent Skills specification](https://agentskills.io/specification) used by
Claude Code, Codex CLI, and ZCode. The implementation is a Python port of
pi/coding-agent's `skills.ts`, adapted to lumen's architecture.

### 12.1 Design principle: skills are not tools

A skill is a **prompt fragment / instruction package**, not an executable
capability. Two read-only loader tools expose discovered skill content without
widening the workspace filesystem boundary. It reaches the model through two
layers:

1. **Progressive disclosure** — only `{name, description, file_path}` appears
   in the system prompt (via `<available_skills>` XML). The full instruction
   body stays on disk until triggered. This keeps idle skills nearly free in
   context budget.

2. **Dual trigger**:
   - **Model-driven**: the model sees the `<available_skills>` catalog, judges
     relevance from the description, and calls `load_skill(name)` to load the
     full body. Relative references are loaded with
     `read_skill_resource(name, path)`, confined to that discovered skill's
     directory.
   - **User-driven**: the user types `/skill:<name> [args]`, which wraps the
     body in a `<skill>` XML block and sends it as a user message.

### 12.2 SKILL.md format

Each skill is a directory containing a `SKILL.md` file with YAML frontmatter:

```yaml
---
name: commit-message          # optional; defaults to directory name
description: Write concise…   # required; 1-1024 chars
disable-model-invocation: false  # optional; true = manual /skill: only
---
Markdown instruction body…
```

Validation rules (porting pi `skills.ts:92-127`):

- `name`: kebab-case `^[a-z0-9]+(-[a-z0-9]+)*$`, 1-64 chars. Falls back to
  parent directory name if omitted. Uppercase is silently lowercased.
- `description`: **required**. Empty → skill is dropped (the model has no way
  to judge relevance).
- `disable-model-invocation`: when `true`, the skill is hidden from the
  `<available_skills>` block but still reachable via `/skill:<name>`.

### 12.3 Discovery

`SkillLoader` scans two directories on startup:

| Path | Source label | Precedence |
|------|-------------|------------|
| `<workspace>/.lumen/skills/` | `project` | Higher (wins on collision) |
| `~/.lumen/skills/` | `user` | Lower |

On a name collision, the project skill wins and a warning is recorded.
Scanning rules mirror pi's `loadSkillsFromDir`: a directory containing
`SKILL.md` is a skill root (stop recursing); top-level `.md` files are also
loaded as standalone skills; `.git`, `node_modules`, `__pycache__`, `.venv`
are pruned.

Controlled by `agent.skills_enabled` (default `true`).

### 12.4 System prompt integration

`ResourceManager._load_instructions()` appends the `<available_skills>` block
after the base + control + project instructions. Only model-invocable skills
appear. The model is told to use `load_skill(name)` for the full instructions
and `read_skill_resource(name, path)` for relative sibling files:

```xml
<available_skills>
  <skill>
    <name>commit-message</name>
    <description>Write concise commit messages…</description>
    <location>/path/to/SKILL.md</location>
  </skill>
</available_skills>
```

### 12.5 Manual invocation

The `/skill:<name> [args]` command handler:

1. Looks up the skill by name via `ResourceManager.get_skill()` (searches all
   skills, including `disable-model-invocation` ones).
2. Expands the body via `expand_skill_for_message()` into a `<skill>` XML
   block with a base-dir note for relative-path resolution.
3. Appends user args after the block.
4. Sends as a user message to `runtime.run()`, after `@path` mention expansion.

### 12.6 TUI integration

- `/skills` — lists all discovered skills with descriptions.
- `/skill:<name>` — manually triggers a skill.
- Completion dropdown includes dynamic `/skill:<name>` entries for every
  discovered skill, so users can discover and invoke from the dropdown.

### 12.7 Build vs. buy

No mature, framework-agnostic Python library implements the full
discover-parse-inject loop:

- `pydantic-ai-skills` — hard-coupled to Pydantic AI, heavy abstractions.
- Official `skills-ref` — validation/lint only, not on PyPI, not for production.
- `skillcheck` — CI linter only, no runtime loading.
- pydantic-ai native `Capability(defer_loading=True)` — supports progressive
  disclosure but does not auto-discover `SKILL.md` from directories.

The self-built loader (`skills.py`, ~250 lines) follows the open spec and
matches the project's existing port-from-pi pattern (`file_search.py`).

## 13. TUI Interaction Redesign [Added 2026-07-22]

A comprehensive interaction and visual upgrade based on analysis of pi/tui's
design document (`pi/packages/tui/DESIGN.md`).

### 13.1 Slash completion race fix

**Root cause**: `PromptEditor.on_key` used `self.dropdown_open` (an
asynchronously-updated derived property) to decide whether Enter means
"accept completion" or "submit". When the user typed fast (`/q` + Enter),
the `CompletionRequested` message hadn't been processed yet, so the dropdown
was still closed → Enter submitted a literal `/q`.

**Fix**: `on_key` now synchronously calls `_current_trigger_token()` to
derive the completion state, eliminating the async race. The decision is
always based on the current cursor position, not a stale flag.

Additionally, `action_select()` now hides the dropdown **synchronously**
(clears `_suggestions` and `display`) before posting the message, preventing
a double-Enter race. A `_suppress_completion` guard prevents the text
mutation from `_replace_token_at_cursor` from immediately re-opening the
dropdown.

### 13.2 Slash trigger symmetry

The old `idx < 0` constraint restricted `/` completion to column 0. Removed
so `/` triggers at any token boundary (after whitespace or at line start),
matching `@` behaviour. `/quit` now works mid-line and after indentation.

### 13.3 Context-sensitive Esc and Ctrl+C

| Key | Context | Action |
|-----|---------|--------|
| `Esc` | Dropdown open | Close dropdown |
| `Esc` | Worker running | Cancel run |
| `Esc` | Editor has text | Clear editor |
| `Esc` | Idle | No-op |
| `Ctrl+C` | Worker running | Cancel run |
| `Ctrl+C` | Idle | Quit app |

This matches pi/tui's overlay/focus state machine: the most "modal" surface
wins. Esc never quits; Ctrl+C only quits when idle.

### 13.4 History navigation

Added `Ctrl+Up` / `Ctrl+Down` for history navigation from any cursor
position (Emacs-style, matching pi/tui's modifier+arrow bindings). Bare
Up/Down still only trigger history at row 0, col 0.

### 13.5 Theme and visual upgrade

- **LUMEN_DARK recalibrated** to GitHub-dark palette: `#0D1117` background,
  `#58A6FF` primary, `#79C0FF` accent, `#E6EDF3` foreground (≥7:1 contrast).
- **Message hierarchy**: user messages get `»` prefix + accent left rule;
  commentary blocks get `secondary` italic; progress blocks get `↳` prefix.
- **ToolCard visual states**: error gets `$error 5%` background; pending gets
  `$warning 5%` background; args block gets `$surface 80%` code feel.
- **Status bar**: three-segment layout with compact token formatting
  (`1.2k` instead of `1234`).
- **Topbar**: simplified — MCP status only shown when unhealthy.

## 14. Robustness Fixes [Added 2026-07-22]

A code audit identified three P0 (critical) and six P1 (important) issues.
All nine have been fixed.

### 14.1 Non-blocking file search (P0)

`search_files` shells out to `fd` (or falls back to `os.walk`) which is
blocking I/O. It was called synchronously from the async `_refresh_completions`
path, freezing the TUI for up to 2 seconds per `@` keystroke.

**Fix**: wrapped in `asyncio.to_thread()` so the subprocess runs in a thread
pool and the event loop stays responsive.

```python
hits = await asyncio.to_thread(
    search_files, prefix, self.resources.registry.workspace
)
```

### 14.2 Iterative compaction wiring (P0)

The `previous_summary` parameter existed in `ContextManager.prepare()` and
`_summarize()` had full iterative-update logic (`_summary_instructions`
handles `<previous-summary>`), but `AgentRuntime.run()` never passed it. Every
compaction rebuilt the summary from scratch — risking drift on long sessions.

**Fix**: three-part wiring:
1. `AgentRuntime.run()` accepts `previous_summary: ContextSummary | None`.
2. The TUI (`app.py`) stores `_last_compaction_summary` after each successful
   run, extracted from `outcome.compaction.summary`.
3. On the next run, it passes the stored summary through to `runtime.run()`.

### 14.3 Provider retry with exponential backoff (P0)

A single 429/503/connection-reset terminated the run with `RunFailed`. No
retry logic existed anywhere in the agent loop.

**Fix**: a retry loop wraps the `run_stream_events` call:
- `_is_transient(error)` classifies errors: connection errors, timeouts, and
  HTTP 408/429/500/502/503/504 are retryable; usage limits, tool truncation,
  and validation errors are not.
- Up to 3 attempts with exponential backoff (1s, 2s, 4s).
- **Critical safety**: retries only happen when `stream_started == False` —
  once any event has been emitted to the timeline, a retry would duplicate
  content. Mid-stream failures propagate immediately.

### 14.4 Streaming flush consistency (P1)

`_do_assistant_flush` (sync timer callback) called
`self._assistant_markdown.update(text)` without `await`, while
`_flush_assistant_now` (async) called `await update(text)`. The sync path
returned an unawaited `AwaitComplete` object — it worked (the internal
`gather()` fires eagerly) but produced a `RuntimeWarning` and lacked
completion-ordering guarantees.

**Fix**: `_do_assistant_flush` now uses `asyncio.create_task()` and stores the
task reference in `_assistant_flush_task` to prevent GC cancellation.

### 14.5 CancelledError propagation (P1)

`_run_prompt` caught `asyncio.CancelledError`, recorded the cancelled turn,
and returned normally — swallowing the cancellation. Since Python 3.8,
`CancelledError` is a `BaseException` that must propagate to the caller.

**Fix**: added `raise` after the cleanup code. The turn is still recorded as
"cancelled" (the cleanup stays), but the cancellation now propagates to the
Textual `Worker`, leaving it in a consistent state.

### 14.6 Plan revision bump on step update (P1)

`TaskController.update_step()` created the new `PlanState` with
`revision=self._state.revision` — copying verbatim instead of incrementing.
Consumers tracking `revision` to detect changes missed step transitions.

**Fix**: `revision=self._state.revision + 1`.

### 14.7 Atomic write temp-file cleanup (P1)

`_atomic_write` created and fsynced a temp file, then the *caller* did
`os.replace`. If `os.replace` failed or the call was cancelled between the
two, the temp file `.{name}.tmp-{suffix}` leaked.

**Fix**: merged into a single `_atomic_replace(parent, name, target, encoded)`
function that owns both the write and the replace. The `except BaseException`
block (catches `CancelledError` too) unlinks the temp file on any failure.

### 14.8 Buffered text flush on exception (P1)

When the stream raised mid-response, the `except` blocks in `run()` did not
flush `response_text_buffer`. A half-streamed answer vanished silently.

**Fix**: all four `except` branches (`CancelledError`, `IncompleteToolCall`,
`UsageLimitExceeded`, `Exception`) now call
`await _flush_response_text(as_final=True)` before emitting the failure event.

### 14.9 Skill location XML escaping (P1)

`format_skills_for_prompt` XML-escaped `description` but not `name` or
`location`. A file path containing `&`, `<`, or `>` would produce malformed
XML in the `<available_skills>` block. `expand_skill_for_message`'s attribute
values had the same issue with `"`.

**Fix**: added `_xml_escape()` and `_xml_attr_escape()` helpers; all
free-text content and attribute values are now escaped.

## 15. Composer-adjacent run controls [Added 2026-07-23]

The composer now owns two stable surfaces immediately above the prompt:

- `RunActivityIndicator` translates structured events into animated public
  activity such as `Reading README.md`, `Searching query in src/`, or
  `Writing outputs/report.md`. It includes elapsed time and tool count, and is
  removed on completion, failure, or cancellation.
- `ApprovalPanel` is a queue-aware vertical selector. Only the active request
  is interactive; later requests wait behind it. The initial state has no
  selection, `Y`/`N` decide directly, and `Up`/`Down` plus `Enter` confirms.
  Timeline `ToolCard`s remain audit records and no longer own focus in the app.

`Shift+Tab` is the primary manual → accept_edits → auto cycle binding (`Ctrl+M`
remains for compatibility). Footer context usage is the provider-neutral
request estimate and remains visible after completion.

The structured backend continues to use `PlanState` and `set_plan`, while the
user-facing panel label is `Todo`. Generated reports, exports, and documents
default to `outputs/<descriptive-name>` when the user gives no explicit path;
source edits continue to target their actual project locations.
