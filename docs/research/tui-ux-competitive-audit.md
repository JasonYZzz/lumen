# Lumen TUI UX Competitive Audit

Date: 2026-08-04

## Scope and method

This audit evaluates the current Lumen terminal interface against Claude Code,
Codex CLI, and pi. It focuses on perceived activity, public reasoning summaries,
tool research, plan review, approvals, composer behavior, long-session navigation,
accessibility risk, and extensibility.

Evidence used:

- Fresh 120×36 Textual renders captured during this audit from the current
  workspace implementation.
- Current Lumen source and interaction tests.
- First-party product documentation and public source code for comparators.

The scores are heuristic product-audit scores, not a controlled usability study
and not a measure of model quality.

## Overall verdict

Lumen is already a capable, state-rich TUI. Its plan and approval lifecycle is
stronger than many lightweight coding-agent shells. The main gap is information
compression: commentary, tool cards, the activity row, plan rows, and the footer
can all communicate overlapping run state at once. Claude Code and Codex are
better at switching between a quiet default view and an inspectable detailed
view; pi is substantially more extensible.

Weighted result:

| Product | Score / 10 | Position |
|---|---:|---|
| Claude Code | 9.1 | Most complete interaction and recovery design |
| Codex CLI | 8.9 | Strongest live-state/transcript architecture |
| pi | 8.0 | Strongest customization and extension surface |
| Lumen | 7.6 | Strong plan/safety foundation; needs information compression |

Weights: hierarchy 12%, activity 10%, reasoning 12%, tools 14%, plan 12%,
approval 12%, composer 10%, long-session recovery 10%, accessibility 5%,
extensibility 3%.

## Scorecard

| Dimension | Lumen | Claude Code | Codex CLI | pi |
|---|---:|---:|---:|---:|
| Visual hierarchy | 7.2 | 8.8 | 8.9 | 7.8 |
| Loading/perceived responsiveness | 8.1 | 8.8 | 9.1 | 8.4 |
| Public reasoning/commentary | 6.7 | 9.2 | 8.6 | 8.2 |
| Tool research trace | 7.6 | 9.0 | 9.2 | 8.7 |
| Plan/task interaction | 8.8 | 9.4 | 8.8 | 6.8 |
| Approval/safety UX | 8.4 | 9.1 | 9.0 | 7.1 |
| Composer/discoverability | 7.7 | 9.5 | 9.2 | 8.7 |
| Long-session navigation/recovery | 6.7 | 9.4 | 9.0 | 8.0 |
| Accessibility/terminal resilience | 6.5 | 8.5 | 8.4 | 7.4 |
| Extensibility | 7.2 | 8.5 | 8.2 | 10.0 |

## Captured flow

### 1. Idle workspace — healthy, but over-spaced

The warm-neutral palette is coherent and the workspace context is explicit.
However, the top bar and welcome block repeat identity/session information, and
the large unused center area makes the first prompt feel visually distant.

Screenshot: `01-idle.svg.png` in the audit artifact folder.

### 2. Thinking and reading — healthy state semantics

The activity row is a real strength: it uses a semantic verb, target path,
elapsed time, and tool count instead of a generic spinner. Public commentary is
distinguished from the final response. The downside is duplication between the
commentary, open tool card, and activity row.

Implementation: `src/lumen/ui/activity_indicator.py:15` and
`src/lumen/ui/event_renderer.py:77`.

Screenshot: `02-thinking-and-reading.svg.png`.

### 3. Tool research result — usable audit trail, too verbose by default

Risk, origin, arguments, result preview, and elapsed time are visible. This is
excellent for debugging and trust. Raw JSON is expensive in the normal reading
path, completed read/search calls are not coalesced, and the persistent
“Reviewing result” row repeats the completed card. A fresh-render screenshot
also showed a `New activity` marker despite the test flow being at the latest
state, which merits a follow-tail regression test.

Screenshot: `03-tool-research.svg.png`.

### 4. Structured plan — healthy and scannable

The `Tasks n/m` structure and distinct completed/pending glyphs make progress
easy to scan. The TUI does not surface dependencies, acceptance criteria,
evidence, revision, or lifecycle even though the underlying plan model has
them. This is appropriate for the default view, but there is no detail view.

Screenshot: `04-structured-plan.svg.png`.

### 5. Plan review — functionally strong, unsafe default emphasis

The gate is stable above the composer and offers clear keyboard/number access.
It directly maps plan approval to execution permission. The highlighted first
choice is `Approve and start in Auto`, which makes the highest-autonomy choice
the accidental Enter path. Default selection should preserve the current
approval mode, usually manual, and Auto should require explicit movement or a
second confirmation.

Implementation: `src/lumen/ui/plan_review_panel.py:45`.

Screenshot: `05-plan-review.svg.png`.

### 6. Tool approval — strong, inspectable, keyboard-first

The diff is next to the decision, risk/origin are visible, and the options cover
once/session/deny. The stable composer-adjacent placement is better than putting
focusable selectors throughout transcript history. Improvements: show the
exact remembered policy scope, add an optional deny-with-feedback path, and use
more explicit risk copy for external/network access.

Screenshot: `06-tool-approval.svg.png`.

Audit artifacts:

`/Users/mac/.codex/visualizations/2026/08/04/019fca54-3dc2-77f2-9e5a-233f361a8d5c/lumen-tui-audit-v2/`

## Competitive comparison

### Loading and live activity

Lumen's semantic activity row is better than a spinner-only design. It maps
tools to verbs such as Reading, Searching, Editing, and Running command, and
shows duration/tool count. Its weakness is that activity remains an independent
surface even when commentary or a tool card already proves progress.

Codex models the bottom activity indicator as derived busy state and explicitly
hides the status row while commentary is streaming to avoid duplicate progress
signals. Its live active cell can mutate while tool work is in progress, and
the transcript overlay includes that live tail. Claude Code similarly offers a
quiet normal view and a detailed transcript/verbose view. pi lets extensions
replace the working message and indicator entirely.

Recommendation: one authoritative live-state reducer should decide whether the
user sees commentary, an active tool row, or a fallback activity row—never all
three for the same event.

### Thinking and public reasoning

Lumen renders every `CommentaryDelta` inline as an italic `∴` block. This is
transparent, but lacks collapse/expand and density modes. The product should
show public reasoning summaries or declared commentary only, not hidden
chain-of-thought.

Claude Code has an explicit extended-thinking toggle and collapsed thinking
blocks; settings can expose thinking summaries, while `Ctrl+O` expands detailed
content. Codex maintains separate reasoning buffers/summary parts and treats
commentary as a distinct live stream. pi supports configurable hidden-thinking
labels and thinking levels.

Recommendation: add `Normal` and `Verbose` transcript density. In Normal,
collapse a commentary segment to a one-line summary; in Verbose, show its
public content. Keep progress receipts separate from reasoning prose.

### Tool research and execution

Lumen has a trustworthy audit trail, but it renders one card per tool and raw
arguments early. Claude Code collapses normal tool usage and exposes a detailed
transcript or verbose view. Codex coalesces adjacent read/list/search shell calls
into an `Exploring` cell; other commands and MCP calls remain separate semantic
cells. Its mutable live tail also appears in the transcript overlay. pi allows
each tool to provide custom `renderCall` and `renderResult` components.

Recommendation: group adjacent read/search/list operations into one
`Explored 7 files · 3 searches · 2.4s` row. Expand with `Ctrl+O` or Enter to see
individual calls. Keep writes, failures, approvals, and verification commands
uncoalesced.

### Plans and approvals

Lumen's host-level plan review is a genuine product advantage: the TUI is
projecting a persisted state machine rather than inventing a local dialog.
Claude Code offers comparable approve-with-mode choices plus direct plan editing
in an external editor. Codex renders approvals as blocking, composer-adjacent
bottom-pane selection views and supports turn, session, exec-policy, and
network-policy decisions.

Recommendation: retain the session's current approval mode as the selected plan
approval choice. Add `Ctrl+G edit plan`, a visible revision, and a detail view
for acceptance criteria/evidence. Feedback should open a focused input labeled
`What should change in revision N?` rather than silently returning to a generic
composer.

### Composer and long sessions

Lumen already supports multiline input, prompt history, kill/yank, external
editor, slash/path autocomplete, queued steering, and follow-up input. Gaps
versus the leaders are reverse history search, Vim/keymap configuration, direct
shell mode, image paste, transcript search, copy-friendly raw output, task-list
toggle, notification/title state, and rewind/checkpoint navigation.

Claude Code provides reverse search, Vim mode, image paste, shell mode,
background tasks, a task-list toggle, transcript viewer, recap, and checkpoint
rewind. Codex exposes transcript/raw modes, configurable keymaps, status line,
terminal title, animations, notifications, and live active cells. pi exposes
most of its header/footer/editor/tool/working UI to extensions.

## Verified Claude Code and Codex TUI interaction appendix

Evidence checked 2026-08-05. `Fact` means first-party documentation or public
source/tests support the claim; `Inference` is our product interpretation.

### Surface and interruption hierarchy

| State | Claude Code CLI — fact | Codex CLI — fact |
|---|---|---|
| Ordinary work | Inline transcript. Classic rendering uses native scrollback; fullscreen rendering uses the alternate screen, fixes the composer at the bottom, and retains only visible messages in its render tree. | Inline committed `HistoryCell`s plus one mutable `active_cell` for streaming work. |
| Tool detail | Normal mode collapses tool detail. `Ctrl+O` opens a transcript viewer with detailed execution, timestamps, and model identity; fullscreen also supports click-to-expand call/result pairs. | Normal cells show bounded output and an omitted-line hint. `Ctrl+T` opens a full-screen transcript overlay containing committed history and the live active-cell tail. |
| Permission / Plan gate | Blocking dialog. `Esc` closes the dialog rather than interrupting the turn. Plan completion offers Auto, accept-edits, manual review, keep-planning, or Ultraplan paths. | Blocking bottom-pane selection view beside the composer. Exec, patch, network, and extra-permission prompts show reason/scope plus numbered decisions. Source/tests use both `approval_overlay` and `approval_modal`; “bottom-pane blocking view” is the precise neutral term. |
| Structured question | `AskUserQuestion` belongs to the dialog/select interaction family, including free-text `Other`. | `RequestUserInputOverlay` is a queued bottom-pane form with options, notes/freeform, question navigation, optional countdown, and unanswered confirmation. |
| Secondary inspection | `/btw` is explicitly a dismissible, ephemeral overlay that can run during the main turn, has no tools, and does not enter history. | Transcript/pager/resume-transcript views are alternate-screen overlays. Status/activity remains inline. |

Sources: [Claude interactive mode](https://code.claude.com/docs/en/interactive-mode),
[Claude fullscreen rendering](https://code.claude.com/docs/en/fullscreen),
[Claude permission modes](https://code.claude.com/docs/en/permission-modes),
[Codex `ChatWidget` at audited commit](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/chatwidget.rs),
[Codex transcript overlay](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/pager_overlay.rs),
[Codex approval view](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/bottom_pane/approval_overlay.rs),
[Codex user-input view](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/bottom_pane/request_user_input/mod.rs).

### Tool grouping and status naming

| Product | Verified default vocabulary/grouping | Audit path |
|---|---|---|
| Claude Code | Read/search calls collapse into groups, using present tense while active (`Reading`, `Searching for`) and past tense when complete (`Read`, `Searched for`). MCP calls can collapse to a summary such as `Called slack 3 times`. `/focus` is an even quieter persistent view: last prompt, one-line tool summary with edit diffstats, final answer. | `Ctrl+O` detailed transcript; fullscreen adds search, expand, export to native scrollback, and open in `$VISUAL`/`$EDITOR`. |
| Codex CLI | Adjacent shell calls parsed entirely as read/list/search coalesce into `Exploring`/`Explored`, with child verbs `Read`, `List`, and `Search`. Web work is `Searching the web`/`Searched the web`; commands are `Running`/`Ran`; MCP is `Calling`/`Called`. Non-user command and MCP previews are limited to five lines. | Omission hints point to the `Ctrl+T` transcript; command transcript rows retain command, output, exit glyph/code, and duration. |

Sources: [Claude transcript controls](https://code.claude.com/docs/en/interactive-mode#transcript-viewer),
[Claude focus/transcript](https://code.claude.com/docs/en/fullscreen#search-and-review-the-conversation),
[Claude official changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md),
[Codex exec grouping](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/exec_cell/model.rs),
[Codex exec rendering](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/exec_cell/render.rs),
[Codex web-search cell](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/history_cell/search.rs),
[Codex MCP cell](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/history_cell/mcp.rs).

Neither CLI has a verified fixed global
`Thinking → Research → Execute → Verify` stage navigator:

- **Fact — Claude:** extended thinking is collapsed by default and appears as
  gray italic text in detailed mode; research/execution appear through actual
  tool rows and dialogs. No universal Verify state was found.
- **Fact — Codex:** runtime/title buckets include `Thinking`, `Working`, and
  `Waiting`; reasoning summaries are dim italic bullets. Research uses
  `Exploring`; execution uses `Running`; plans use `Proposed Plan` / `Updated
  Plan` with pending, in-progress, and completed items. No universal Verify
  state was found.
- **Inference:** both are event/tool-driven progressive-disclosure UIs, not
  fixed workflow steppers. Verification normally looks like another test/Bash
  tool call unless the host emits typed verification state. Lumen should only
  show `Verifying` when backed by an explicit host event or typed plan step.

Sources: [Claude extended thinking](https://code.claude.com/docs/en/model-config#extended-thinking),
[Codex status state](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/chatwidget/status_state.rs),
[Codex reasoning rendering](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/history_cell/messages.rs),
[Codex plan rendering](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/history_cell/plans.rs).

### Color and accessibility strategy

| Area | Claude Code — fact | Codex CLI — fact / limit |
|---|---|---|
| Theme/color | `/theme` supports auto light/dark, dark/light, daltonized and ANSI presets, plus custom/plugin themes with token overrides and hot reload. | `/theme` supports bundled/custom `.tmTheme`, live preview, cancel restore, and persistence. Rendering probes foreground/background and truecolor/256/16-color support, then quantizes or falls back. |
| Motion | `prefersReducedMotion` reduces or disables spinner, shimmer, and flash effects. | Central `MotionMode` supplies explicit fallbacks: hidden or static bullet; shimmer becomes plain text. |
| Non-color/accessibility | `--ax-screen-reader` replaces boxes, animation, and redraws with labeled linear text; it promises no color-only cues and turns menus into numbered lists. Daltonized themes and magnifier cursor tracking are documented. | State usually combines color with words/glyphs; low-color charts switch from gradients to `□`/`■`. CJK/wide-character and resize tests exist. No equivalent dedicated screen-reader mode was found in the inspected public TUI source/config, so this is not screen-reader parity evidence. |

Sources: [Claude terminal themes](https://code.claude.com/docs/en/terminal-config#match-the-color-theme),
[Claude accessibility](https://code.claude.com/docs/en/accessibility),
[Codex theme picker](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/theme_picker.rs),
[Codex motion fallbacks](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/motion.rs),
[Codex terminal palette](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/terminal_palette.rs),
[Codex low-color glyph fallback](https://github.com/openai/codex/blob/e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a/codex-rs/tui/src/chatwidget/tokens/chart/palette.rs).

Evidence boundary: Claude's public repository does not expose its full core TUI,
so its entries are documented external behavior, not claims about private
components. Codex source claims are pinned to commit
`e87e2b495bcf8aa1950e2bb24cc95bfdc6fd473a`. The four-stage mapping above is
explicitly analytical, not a competitor API or state-machine claim.

## P0 recommendations

1. Add `Normal/Verbose` transcript density and coalesce adjacent read-only tool
   calls. Default to Normal.
2. Replace independent activity updates with a single derived live-state
   reducer; hide fallback activity while visible commentary/tool streaming is
   active.
3. Change Plan Review's default selection from Auto to the current approval
   mode. Require explicit confirmation for escalation to Auto.
4. Make commentary segments collapsible. Display public summaries, not hidden
   reasoning, and persist the expanded/collapsed choice per session.
5. Compress the idle welcome view and remove repeated agent/session identity.
   Move detailed resources/session/output paths behind `/status`.
6. Fix follow-tail false positives and add resize/reflow regression tests for
   `New activity`.
7. Add an `animations` setting, a static reduced-motion indicator, contrast
   checks, and CJK/wide-character snapshot coverage.

## P1 recommendations

1. Add a transcript overlay with search, raw copy mode, per-turn navigation,
   and expand-all tool/commentary content.
2. Add checkpoint/rewind UI backed by trusted file receipts and session state.
3. Add a task/child-run panel with running/waiting/failed status, owner,
   duration, and cancellation; support terminal title and desktop notification
   for action-required/completed states.
4. Add reverse history search, configurable keymaps/Vim mode, direct shell
   input, and optional image paste.
5. Make status-line fields configurable and add OSC 8 links for files,
   branches, commits, and child runs when terminal support is detected.
6. Provide tool-specific compact renderers and progressive output for commands,
   while retaining the full receipt in Verbose/transcript view.

## Accessibility and evidence limits

- Keyboard access is strong, but focus visibility and selection rely heavily on
  low-percentage background tints. Contrast should be measured in both themes.
- Animation runs at 120 ms per frame with no user-facing reduced-motion setting.
- The PNG conversion pipeline showed tight CJK/Latin spacing in some rows. This
  may be a Quick Look font-width artifact; it must be verified in iTerm2,
  Terminal.app, Kitty, WezTerm, VS Code, and Windows Terminal before treating it
  as a production defect.
- Screenshots cannot confirm screen-reader behavior, terminal title updates,
  notification delivery, mouse selection, or latency under real provider
  streaming.

## Primary sources

- Claude Code interactive mode:
  https://code.claude.com/docs/en/interactive-mode
- Claude Code permission/plan modes:
  https://code.claude.com/docs/en/permission-modes
- Claude Code thinking/display settings:
  https://code.claude.com/docs/en/settings
- Claude Code checkpointing:
  https://code.claude.com/docs/en/checkpointing
- Claude Code status line:
  https://code.claude.com/docs/en/statusline
- Codex TUI `ChatWidget` source:
  https://github.com/openai/codex/blob/main/codex-rs/tui/src/chatwidget.rs
- Codex configuration schema:
  https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json
- Codex app-server approvals/events:
  https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
- pi coding-agent README:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md
- pi extension UI/tool rendering:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
- pi settings:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md
