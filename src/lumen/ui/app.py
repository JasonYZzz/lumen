from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from typing import ClassVar, cast

from pydantic_ai.messages import ModelMessage
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.lazy import Lazy
from textual.widgets import Static
from textual.worker import Worker

from lumen.approval import ApprovalMode, ApprovalPolicy
from lumen.branding import FRAMEWORK_NAME
from lumen.config import AppConfig
from lumen.context import ContextSummary
from lumen.events import ApprovalRequest, RunEvent, UsageUpdated
from lumen.interactive_queue import QueueLimitError, QueueMode
from lumen.plan import PlanState
from lumen.resources import ResourceManager
from lumen.run_coordinator import RunCoordinator, RunInput
from lumen.runtime import ToolApproval
from lumen.sessions import SessionMetadata
from lumen.timeline import TimelineStore
from lumen.tools.workspace import Workspace
from lumen.ui.activity_indicator import RunActivityIndicator
from lumen.ui.approval_controller import ApprovalControllerMixin
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.autocomplete import CompletionDropdown
from lumen.ui.commands import LumenCommandProvider
from lumen.ui.completion_controller import CompletionControllerMixin
from lumen.ui.composer import ComposerHistory, PromptEditor
from lumen.ui.context_report import format_context_result as _format_context_result
from lumen.ui.event_renderer import EventRendererMixin
from lumen.ui.file_mention import expand_file_mentions

# ``search_files`` is re-exported so tests can monkeypatch
# ``lumen.ui.app.search_files``; the completion controller resolves it
# through this module at call time.
from lumen.ui.file_search import FileSearchHandle
from lumen.ui.file_search import search_files as search_files
from lumen.ui.plan_panel import PlanPanel
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.queue_panel import InteractiveQueuePanel
from lumen.ui.scroll_follow import ScrollFollowMixin
from lumen.ui.session_restore import SessionRestoreMixin
from lumen.ui.slash_handlers import SlashHandlersMixin
from lumen.ui.status_bar import StatusBarMixin
from lumen.ui.streaming_markdown import AssistantMarkdown, StreamingMarkdownController
from lumen.ui.themes import register_themes, theme_color
from lumen.ui.tool_card import ToolCard
from lumen.ui.welcome import WelcomePanel

_PROMPT_KEYWORD = re.compile(r"(?<!\S)(@[\w./-]+|/[a-z][\w:-]*|(?:[\w.-]+/)+[\w.-]+)")


def _highlight_prompt(
    value: str,
    *,
    command_color: str = "#F0A24A",
    path_color: str = "#69B9AF",
) -> Text:
    """Emphasize mentions, slash commands, and paths without parsing markup."""

    rendered = Text("» ")
    cursor = 0
    for match in _PROMPT_KEYWORD.finditer(value):
        rendered.append(value[cursor : match.start()])
        token = match.group(0)
        color = command_color if token.startswith("/") else path_color
        rendered.append(token, style=f"bold underline {color}")
        cursor = match.end()
    rendered.append(value[cursor:])
    return rendered


class LumenApp(
    StatusBarMixin,
    SessionRestoreMixin,
    CompletionControllerMixin,
    ApprovalControllerMixin,
    EventRendererMixin,
    ScrollFollowMixin,
    SlashHandlersMixin,
    App[None],
):
    TITLE = FRAMEWORK_NAME
    # The command palette provider is registered in __init__ via
    # COMMANDS_CLASS so the palette (Ctrl+P) can discover our commands.
    BINDINGS: ClassVar[list[BindingType]] = [
        # Esc is context-sensitive (see action_smart_escape): closes the
        # completion dropdown first, then cancels a running worker, then
        # clears the editor. It never quits the app.
        Binding("escape", "smart_escape", "Close/Cancel", show=True),
        # Ctrl+C is also context-sensitive: cancels a running worker if one
        # is active, otherwise quits the app. This prevents accidental exits
        # during long-running tool calls.
        Binding("ctrl+c", "smart_cancel_or_quit", "Cancel/Quit", show=True),
        Binding("end", "follow_tail", "Latest", show=False, priority=True),
        # Ctrl+P opens the Textual-native command palette, populated by
        # LumenCommandProvider. This replaces /help as the primary
        # command-discovery surface.
        Binding("ctrl+p", "command_palette", "Command palette", show=True),
        Binding("alt+c", "copy_last_response", "Copy last response", show=False),
        Binding(
            "shift+tab",
            "toggle_approval_mode",
            "Cycle permission mode",
            show=False,
            priority=True,
        ),
    ]
    CSS = """
    /* Thin tinted scrollbars (posting pattern). The 1-cell width keeps the
       timeline visually quiet. Tinted with $primary so chrome reads as
       themed, not Textual-default grey. */
    * {
        scrollbar-size-vertical: 1;
        scrollbar-color: $primary 30%;
        scrollbar-color-hover: $primary 70%;
        scrollbar-color-active: $primary;
        scrollbar-background: $background;
    }

    Screen { layout: vertical; background: $background; }

    /* Textual includes borders in explicit heights. Two rows preserve one
       visible information row plus the bottom divider. */
    #topbar {
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text-muted;
    }

    /* Message timeline: minimal chrome so content carries the structure.
       Generous horizontal padding for readability on wide terminals. */
    #messages {
        height: 1fr;
        padding: 0 2 1 2;
        background: $background;
    }

    /* --- Message type styling -------------------------------------------
       Content, glyphs, spacing, and tone carry hierarchy. Container chrome
       is reserved for user input so the transcript reads as one surface. */

    /* User message: surface-tinted bubble with a left accent rule. The »
       prefix is added in code, not CSS, to stay markup-safe. */
    .user-message {
        margin: 1 0;
        padding: 0 1 0 1;
        background: $surface 60%;
        border-left: outer $accent;
        color: $text;
    }
    /* System message: muted, indented — it's metadata, not content. */
    .system-message {
        margin: 1 0;
        padding: 0 1;
        color: $text-muted;
    }
    /* Assistant Markdown: no border, no background. The rendered prose IS
       the focal point. A top margin separates it from preceding blocks. */
    .assistant-message { margin: 1 0; }
    /* Incremental streaming: a segment is a container of frozen Markdown
       blocks plus an active tail. Blocks carry no outer margin so they read
       as continuous prose; the container's ``.assistant-message`` margin
       supplies the segment's outer spacing. A single rich Markdown document
       puts one blank line between top-level blocks; lists/quotes/tables
       render that blank line themselves, so only the remaining blocks get
       ``--spaced`` (see streaming_markdown._needs_top_margin). */
    .assistant-block { margin: 0; }
    .assistant-block--spaced { margin-top: 1; }
    /* Commentary (intermediate model reasoning): visually de-emphasised.
       It's analysis the user can skim, not the final answer. */
    .commentary-block {
        margin: 1 0;
        padding: 0 2;
        color: $text-muted;
        text-style: italic;
    }
    /* Progress block: accent-coloured, distinct from commentary. The ↳
       prefix is added in code. */
    .progress-block {
        margin: 1 0;
        padding: 0 1;
        color: $accent;
    }
    .compaction-row { margin: 1 0; color: $text-muted; }
    .restored-notice {
        margin: 1 0;
        padding: 0 1;
        color: $success;
    }
    .welcome-panel {
        width: 100%;
        max-width: 110;
        height: auto;
        margin: 1 0 2 0;
        padding: 1 2;
        color: $text-muted;
        background: $background;
    }

    /* --- Composer ------------------------------------------------------- */
    /* A single rounded prompt box. Enter submits (documented in the status
       line). The focus ring is the primary affordance: nearly invisible
       when unfocused, clear accent border when focused. */
    #prompt {
        height: 3;
        margin: 0 2;
        border: round $primary 35%;
        padding: 0 1;
        background: $surface;
        color: $text;
    }
    #prompt:focus {
        border: round $accent 60%;
        background: $surface 100%;
    }
    /* The TextArea's own cursor — themed via variables in themes.py. */

    /* --- Status bar ----------------------------------------------------- */
    /* Mode context belongs to the composer, matching Claude Code's prompt
       footer. It stays visually quiet until a higher-autonomy mode is on. */
    #status {
        height: 1;
        margin: 0 2 1 2;
        padding: 0 1;
        background: $background;
        color: $text-muted;
    }
    #new-activity {
        display: none;
        height: 1;
        padding: 0 2;
        color: $accent;
        background: $surface;
        text-align: right;
    }
    #new-activity.visible { display: block; }
    """

    # Register our command palette provider so Ctrl+P surfaces Lumen's
    # commands (session/model/tool/run/app) alongside Textual's built-ins.
    COMMANDS: ClassVar[set[type] | type] = {LumenCommandProvider}  # type: ignore[assignment]

    # Back-compat seam: the ``/context`` zone-table formatter lives in
    # ``context_report.py``; existing callers and tests use this staticmethod.
    format_context_result = staticmethod(_format_context_result)

    def __init__(
        self,
        config: AppConfig,
        resources: ResourceManager,
        *,
        resume_id: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.resources = resources
        self.resume_id = resume_id
        self.session: SessionMetadata | None = None
        self.history: list[ModelMessage] = []
        self.plan: PlanState = PlanState()
        self.current_worker: Worker[None] | None = None
        self.last_prompt: str | None = None
        # Permission modes mirror Claude Code's CLI cycle. Legacy "ask"
        # configuration is normalized to manual.
        # ``external_unknown`` remains approval-gated because it represents a
        # remote capability the operator has not classified. Initialised from
        # config but live-toggled via /mode or Shift+Tab.
        self._approval_mode = ApprovalMode.parse(config.permissions.default_mode)
        self._approval_policy = ApprovalPolicy()
        self._assistant_stream: StreamingMarkdownController | None = None
        # One widget represents one logical assistant Markdown document.
        self._assistant_container: AssistantMarkdown | None = None
        self._commentary_container: Static | None = None
        self._commentary_text = ""
        # The active plan belongs to the current user turn and lives inside
        # the scrolling transcript. Older turns retain their own plan panel.
        self._active_plan_panel: PlanPanel | None = None
        self._last_assistant_output = ""
        self._session_approval_keys: set[str] = set()
        # Tool cards are mounted into the message timeline, keyed by call id so
        # multiple updates to one call render into a single card.
        self._tool_cards: dict[str, ToolCard] = {}
        # Pending approval futures: one per call id. The runtime approval
        # callback awaits the future; the card's Decision message resolves it.
        self._approval_waiters: dict[str, asyncio.Future[ToolApproval]] = {}
        self._compaction_row: Static | None = None
        # Maximum number of tool cards kept in the _tool_cards dict. Old cards
        # are pruned to prevent unbounded widget-tree growth in long sessions.
        # The widgets themselves stay in the timeline (Lazy-rendered), but
        # the dict that tracks them for updates is capped.
        self._TOOL_CARD_MAX = 100
        # Last compaction's summary, carried forward for iterative compaction.
        # When the next run triggers compaction, this is passed as
        # ``previous_summary`` so the summarizer merges new history into the
        # prior summary rather than rebuilding from scratch (prevents drift).
        self._last_compaction_summary: ContextSummary | None = None
        self.coordinator = RunCoordinator(
            repository=resources.session_repository,
            agent_name=config.agent.name,
            model_id=lambda: resources.active_model_config().id,
            runtime=lambda: resources.runtime,
        )
        self.timeline_store = TimelineStore()
        self._follow_tail = True
        self._last_tail_scroll_y = 0.0
        self._tail_follow_generation = 0
        self._loading_older = False
        # Prompt history (only real prompts, not / commands). Capped at 50.
        self._composer_history = ComposerHistory(max_entries=50)
        # Monotonic completion request id. Slow filesystem searches complete
        # on worker threads; only the newest request may update the dropdown.
        self._completion_generation = 0
        self._completion_search_handle: FileSearchHandle | None = None
        self._last_usage_event: UsageUpdated | None = None

    # Public read-only views for tests/UI introspection. We expose the history
    # list and browsing index so tests can assert navigation without poking at
    # underscore-prefixed attributes.
    @property
    def prompt_history(self) -> list[str]:
        return list(self._composer_history.entries)

    @property
    def history_browsing_index(self) -> int | None:
        return self._composer_history.index

    def seed_history(self, entries: list[str]) -> None:
        """Replace the prompt history with ``entries`` (test/setup helper).

        Production code records history via :meth:`_record_history` as prompts
        are sent. Tests use this to set up a known history stack without
        having to drive a full agent run.
        """

        self._composer_history.seed(entries)

    def compose(self) -> ComposeResult:
        yield Static(f"◆ {FRAMEWORK_NAME}  ·  starting workspace", id="topbar", markup=False)
        yield VerticalScroll(
            WelcomePanel(),
            id="messages",
        )
        yield Static("New activity ↓", id="new-activity", markup=False)
        # Stable run controls live directly above the composer. Activity is a
        # compact animated line; approvals replace it with a queue-aware
        # vertical selector without inserting transient controls in history.
        yield RunActivityIndicator()
        yield ApprovalPanel(workspace=self.resources.workspace)
        yield PlanReviewPanel()
        yield InteractiveQueuePanel()
        # Composer is a single rounded prompt box. Enter submits, so there is
        # no Send button — the focus ring is the only affordance, matching the
        # posting/opencode input style.
        yield PromptEditor(id="prompt", language=None)
        # Claude-style prompt footer: one persistent mode indicator and the
        # minimum useful runtime context. Shift+Tab is the sole key-cycle.
        yield Static(
            "⏸ manual mode on (shift+tab to cycle)",
            id="status",
            markup=False,
        )
        # No Textual Footer: the #status bar above already carries the keymap
        # hint, and the Footer widget would duplicate it in the bottom-right
        # corner (the user explicitly called this out as visual noise).
        # Floating completion dropdown. Lives on its own layer so it can paint
        # over the composer; hidden until the user types @ or /.
        yield CompletionDropdown()

    async def on_mount(self) -> None:
        # Register our builtin themes before any widgets render so the CSS
        # ``$surface`` / ``$accent`` / etc. tokens resolve against the
        # configured palette from the first paint. ``config.ui.theme`` selects
        # the active theme (validated at load; defaults to lumen-dark).
        register_themes(cast(App[object], self), theme=self.config.ui.theme)
        try:
            # Wire the dropdown to the editor so keystrokes can be routed.
            editor = self.query_one("#prompt", PromptEditor)
            dropdown = self.query_one(CompletionDropdown)
            editor.bind_dropdown(dropdown)
            await self.resources.open()
            if self.resume_id:
                state = self.coordinator.resume(self.resume_id)
                restored = True
            else:
                state = self.coordinator.new_session()
                restored = False
            await self._apply_coordinator_state(state, restored=restored)
            self._refresh_topbar()
            # Initialise the status bar with the real model id + mode suffix
            # (the compose placeholder shows generic text until this runs).
            status = self.query_one("#status", Static)
            self._refresh_mode_classes(status)
            status.update(self._status("Ready"))
            if self.resources.warnings:
                await self._append_system("\n".join(self.resources.warnings))
            self.set_interval(0.2, self._load_older_if_at_top)
            self.query_one("#prompt", PromptEditor).focus()
        except Exception as error:
            await self._append_system(f"Startup error: {error}")
            # Even on error, try to refresh the status/topbar with whatever
            # info we have rather than showing a generic "Startup failed" —
            # the user needs to see the model/mode to diagnose the issue.
            try:
                self._refresh_topbar()
                self.query_one("#status", Static).update(self._status("Startup error"))
            except Exception:
                pass

    async def on_unmount(self) -> None:
        if self.current_worker is not None and self.current_worker.is_running:
            self.current_worker.cancel()
            try:
                await self.current_worker.wait()
            except Exception:
                pass
        if self._assistant_stream is not None:
            await self._assistant_stream.close()
            self._assistant_stream = None
        # Reject any pending approval futures so the runtime doesn't deadlock.
        for future in self._approval_waiters.values():
            if not future.done():
                future.set_result(ToolApproval(approved=False, message="session closed"))
        self._approval_waiters.clear()
        await self.resources.close()

    async def _append_system(self, text: str) -> None:
        container = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(container)
        # markup=False is critical: system messages often contain tool/MCP
        # output with ``key: value`` or ``[bracket]`` patterns that Textual's
        # rich-markup parser would misread as style tags and crash on.
        await container.mount(Static(text, classes="system-message", markup=False))
        await self._finish_timeline_update(container, follow)

    async def _append_user(self, text: str) -> None:
        container = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(container)
        # Prefix with » to visually mark user input. markup=False is critical:
        # user text can contain anything (paths, code, colons). The » is a
        # safe ASCII-range character that renders in all terminals.
        app = cast(App[object], self)
        rendered = _highlight_prompt(
            text,
            command_color=theme_color(app, "activity", "#F0A24A"),
            path_color=theme_color(app, "mode-edit", "#69B9AF"),
        )
        await container.mount(Lazy(Static(rendered, classes="user-message", markup=False)))
        await self._finish_timeline_update(container, follow)

    async def _append_commentary(self, text: str) -> None:
        if not text.strip():
            return
        container = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(container)
        if self._commentary_container is None:
            self._commentary_container = Static("", classes="commentary-block", markup=False)
            self._commentary_text = "∴ "
            await container.mount(Lazy(self._commentary_container))
        self._commentary_text += text
        self._commentary_container.update(self._commentary_text)
        await self._finish_timeline_update(container, follow)

    def _close_commentary_segment(self) -> None:
        self._commentary_container = None
        self._commentary_text = ""

    async def _append_progress(self, summary: str, next_action: str | None) -> None:
        # Prefix with ↳ to distinguish progress from commentary and assistant
        # text. The next-action line keeps its → arrow for the sub-bullet.
        body = f"↳ {summary}" + (f"\n\n→ {next_action}" if next_action else "")
        container = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(container)
        await container.mount(Lazy(AssistantMarkdown(body, classes="progress-block")))
        await self._finish_timeline_update(container, follow)

    @on(PromptEditor.Submitted)
    async def prompt_submitted(self, event: PromptEditor.Submitted) -> None:
        if event.mode is QueueMode.STEER and event.model_prompt == event.text:
            await self.handle_input(event.text)
            return
        await self.handle_input(
            event.text,
            queue_mode=event.mode,
            model_prompt=event.model_prompt,
        )

    @on(PromptEditor.DequeueRequested)
    async def dequeue_requested(self) -> None:
        messages = await self.coordinator.dequeue_interactive()
        if not messages:
            self.notify("No queued messages to restore", timeout=2)
            return
        editor = self.query_one("#prompt", PromptEditor)
        queued = "\n\n".join(message.text for message in messages)
        editor.text = "\n\n".join(part for part in (queued, editor.text) if part.strip())
        editor.cursor_location = editor.document.end
        self._refresh_interactive_queue()

    async def handle_input(
        self,
        text: str,
        *,
        queue_mode: QueueMode = QueueMode.STEER,
        model_prompt: str | None = None,
        is_retry: bool = False,
    ) -> None:
        if text.startswith("/"):
            await self._handle_command(text)
            return
        if self.current_worker is not None and self.current_worker.is_running:
            expanded = expand_file_mentions(
                model_prompt if model_prompt is not None else text,
                Workspace(self.resources.workspace),
            )
            expanded = self._apply_permission_mode_context(expanded)
            try:
                await self.coordinator.enqueue_interactive(
                    RunInput(display_text=text, model_prompt=expanded),
                    queue_mode,
                )
            except (QueueLimitError, RuntimeError) as error:
                editor = self.query_one("#prompt", PromptEditor)
                editor.text = text
                editor.cursor_location = editor.document.end
                await self._append_system(str(error))
            else:
                self._record_history(text)
                self._refresh_interactive_queue()
            return
        # Record into prompt history before expansion so /retry and ↑/↓ show
        # the user's literal input (with @path tokens intact).
        self.last_prompt = text
        self._record_history(text)
        # Display the user's literal text in the timeline (with @path), then
        # expand mentions for the model-facing prompt.
        await self._append_user(text)
        # Build a Workspace view over the resource manager's workspace root so
        # @path mentions go through the same escape checks as the file tools.
        expanded = expand_file_mentions(
            model_prompt if model_prompt is not None else text,
            Workspace(self.resources.workspace),
        )
        self.current_worker = self.run_worker(
            self._run_prompt(RunInput(display_text=text, model_prompt=expanded, is_retry=is_retry)),
            name="agent-run",
            exclusive=True,
        )

    def _record_history(self, text: str) -> None:
        """Append ``text`` to prompt history, capped at ``_HISTORY_MAX``.

        We deduplicate against the most recent entry so pressing Enter twice
        on the same prompt doesn't flood the history.
        """

        self._composer_history.record(text)

    def _run_is_active(self) -> bool:
        """Whether an agent-run worker is currently executing."""
        return self.current_worker is not None and self.current_worker.is_running

    async def _cancel_active_run(self) -> None:
        """Request cancellation of the active run worker, if any.

        Used by the command gate before /exit so the run tears down cleanly
        (flushing buffered text, persisting a cancelled turn) rather than being
        abandoned mid-write.
        """
        queued = await self.coordinator.dequeue_interactive()
        if queued:
            editor = self.query_one("#prompt", PromptEditor)
            restored = "\n\n".join(message.text for message in queued)
            editor.text = "\n\n".join(part for part in (restored, editor.text) if part.strip())
            editor.cursor_location = editor.document.end
            self._refresh_interactive_queue()
        worker = self.current_worker
        if worker is not None and worker.is_running:
            worker.cancel()
            # Wait briefly for the worker to observe cancellation so the
            # subsequent exit() / state change doesn't race the tear-down.
            try:
                await worker.wait()
            except Exception:
                pass

    async def _run_prompt(self, run_input: RunInput) -> None:
        if self.resources.runtime is None or self.session is None:
            await self._append_system("Runtime is not available.")
            return

        async def emit(event: RunEvent) -> None:
            await self.render_event(event)

        async def approve(request: ApprovalRequest) -> ToolApproval:
            return await self._await_inline_approval(request)

        async def approve_batch(
            requests: tuple[ApprovalRequest, ...],
        ) -> dict[str, ToolApproval]:
            return await self._await_inline_approval_batch(requests)

        effective_input = RunInput(
            display_text=run_input.display_text,
            model_prompt=self._apply_permission_mode_context(run_input.model_prompt),
            is_retry=run_input.is_retry,
        )
        try:
            outcome = await self.coordinator.run(effective_input, emit, approve, approve_batch)
        except asyncio.CancelledError:
            self._resolve_all_pending_approvals(approved=False, message="run cancelled")
            raise
        else:
            if outcome is None:
                self._resolve_all_pending_approvals(approved=False, message="run failed")
            await self._apply_coordinator_state(self.coordinator.state)
        finally:
            self._refresh_topbar()

    def _apply_permission_mode_context(self, prompt: str) -> str:
        """Tell the model the live mode without exposing the note in the UI."""

        guidance = {
            ApprovalMode.MANUAL: (
                "Plan mode is off. Follow the user's request normally; the host will ask the "
                "user before approval-gated operations."
            ),
            ApprovalMode.ACCEPT_EDITS: (
                "Plan mode is off. Follow the user's request normally. Builtin file edits may "
                "run automatically; other approval-gated operations may pause."
            ),
            ApprovalMode.PLAN: (
                "Work read-only. Explore and return a proposed implementation plan. You may use "
                "read tools and commands that only inspect local state. Do not edit files, run "
                "mutating commands, or affect external systems. Before the final response, call "
                "set_plan with the proposed implementation steps. Keep those future steps pending; "
                "the UI will ask the user to approve them before execution."
            ),
            ApprovalMode.AUTO: (
                "Plan mode is off. Follow the user's request normally. Classified operations may "
                "run without prompts, but continue to obey every boundary stated by the user."
            ),
        }[self._approval_mode]
        return (
            f'<permission-mode name="{self._approval_mode.value}">\n'
            f"{guidance}\n"
            "This current-mode note supersedes permission-mode notes from earlier turns.\n"
            "</permission-mode>\n\n"
            f"{prompt}"
        )

    def action_cancel_run(self) -> None:
        # Reject every pending approval so the worker can actually exit.
        self._resolve_all_pending_approvals(approved=False, message="run cancelled")
        if self.current_worker is not None and self.current_worker.is_running:
            self.run_worker(
                self._cancel_active_run(),
                name="cancel-agent-run",
                exclusive=False,
            )
        else:
            self.query_one("#prompt", PromptEditor).focus()

    def _refresh_interactive_queue(self) -> None:
        runtime = self.resources.runtime
        messages = runtime.interactive_queue.snapshot() if runtime is not None else ()
        self.query_one(InteractiveQueuePanel).update_messages(messages)

    async def action_safe_quit(self) -> None:
        """Route every palette quit through the run-aware command gate."""

        await self._handle_command("/exit")

    def action_follow_tail(self) -> None:
        """Return to the newest activity and re-enable automatic following."""

        self._follow_tail = True
        self._scroll_timeline_end(self.query_one("#messages", VerticalScroll))
        self.query_one("#new-activity", Static).remove_class("visible")

    def action_copy_last_response(self) -> None:
        """Copy the latest complete assistant response via OSC 52 and pbcopy."""

        text = self._last_assistant_output
        if not text:
            self.notify("No assistant response to copy", severity="warning", timeout=2)
            return
        self.copy_to_clipboard(text)
        # Textual's OSC 52 path works in most terminals but Apple Terminal
        # intentionally ignores it. pbcopy makes the same action reliable on
        # macOS without invoking a shell or exposing the response in argv.
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ("pbcopy",),
                    input=text,
                    text=True,
                    check=True,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        self.notify("Copied latest response", timeout=2)

    def action_smart_escape(self) -> None:
        """Context-sensitive Escape: close dropdown → cancel run → clear input.

        Priority order mirrors pi/tui's overlay/focus state machine: the most
        "modal" surface wins. Escape never quits the app — that's Ctrl+C's
        job when idle.

        1. Completion dropdown open → close it (return focus to editor).
        2. Active worker running → cancel it.
        3. Editor has text → clear it.
        4. Idle → no-op.
        """

        # 1. Close the completion dropdown if it's showing.
        try:
            dropdown = self.query_one(CompletionDropdown)
        except Exception:
            dropdown = None
        if dropdown is not None and dropdown.is_open:
            dropdown.hide()
            self.query_one("#prompt", PromptEditor).focus()
            return
        # 2. Cancel a running worker.
        if self.current_worker is not None and self.current_worker.is_running:
            self.action_cancel_run()
            return
        # 3. Clear the editor if it has content.
        editor = self.query_one("#prompt", PromptEditor)
        if editor.text:
            editor.text = ""
            return
        # 4. Idle — no-op. Focus stays on the editor.

    def action_smart_cancel_or_quit(self) -> None:
        """Ctrl+C: cancel a running worker, or quit when idle.

        During a run, Ctrl+C cancels (matching pi/tui's Ctrl+C = cancel, not
        exit). When idle, it quits the app so the user always has a way out.
        """

        if self.current_worker is not None and self.current_worker.is_running:
            self.action_cancel_run()
        else:
            self.exit()

    # -- approval mode -----------------------------------------------------

    @property
    def approval_mode(self) -> str:
        """Public read-only view of the current approval mode."""

        return self._approval_mode.value

    def set_approval_mode(self, mode: str) -> None:
        """Switch the session permission mode immediately."""

        try:
            parsed = ApprovalMode.parse(mode)
        except ValueError as error:
            raise ValueError(
                f"approval mode must be 'manual', 'accept_edits', 'plan', or 'auto', got {mode!r}"
            ) from error
        if parsed is self._approval_mode:
            return
        self._approval_mode = parsed
        if parsed is not ApprovalMode.PLAN:
            try:
                self.query_one(PlanReviewPanel).hide()
            except Exception:
                pass
        self._refresh_topbar()
        # Refresh the status bar suffix too so the mode badge updates live,
        # not just on the next run event.
        try:
            status = self.query_one("#status", Static)
            self._refresh_mode_classes(status)
            status.update(self._status("Ready"))
        except Exception:
            pass

    def request_approval_mode(self, mode: str) -> None:
        """Switch modes directly; the footer is the only success feedback."""

        self.set_approval_mode(ApprovalMode.parse(mode).value)

    _request_approval_mode = request_approval_mode

    def action_toggle_approval_mode(self) -> None:
        """Cycle modes in the same order as Claude Code's CLI."""

        modes = (
            ApprovalMode.MANUAL,
            ApprovalMode.ACCEPT_EDITS,
            ApprovalMode.PLAN,
            ApprovalMode.AUTO,
        )
        index = modes.index(self._approval_mode)
        self._request_approval_mode(modes[(index + 1) % len(modes)].value)

    # -- command palette actions ------------------------------------------
    # These wrap the existing /-command logic as ``action_*`` methods so the
    # LumenCommandProvider can hand them to the Textual command palette.
    # Each is a thin re-entry into the corresponding /command handler,
    # keeping a single source of truth for the actual behaviour.

    async def action_new_session(self) -> None:
        await self._handle_command("/new")

    async def action_list_sessions(self) -> None:
        await self._handle_command("/sessions")

    async def action_prompt_resume(self) -> None:
        """Prompt the user for a session UUID and resume it.

        We use the command palette's own input by asking the user to type
        ``/resume <uuid>``; for now this surfaces a hint rather than a modal
        input, since Textual's command palette doesn't support argument
        prompting natively.
        """

        await self._append_system("Type /resume <session-uuid> in the prompt, or /sessions to list them.")

    async def action_switch_model(self, model_name: str) -> None:
        await self._handle_command(f"/model {model_name}")

    async def action_list_tools(self) -> None:
        await self._handle_command("/tools")

    async def action_list_hooks(self) -> None:
        await self._handle_command("/hooks")

    async def action_clear_timeline(self) -> None:
        await self._handle_command("/clear")

    async def action_show_history(self) -> None:
        if not self._composer_history.entries:
            await self._append_system("No prompt history yet.")
            return
        rows = [f"{i + 1}.  {entry}" for i, entry in enumerate(reversed(self._composer_history.entries))]
        await self._append_system("\n".join(rows))

    async def action_retry_last(self) -> None:
        if self.last_prompt is None:
            await self._append_system("There is no previous prompt to retry.")
            return
        await self.handle_input(self.last_prompt, is_retry=True)
