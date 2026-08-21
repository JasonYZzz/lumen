# LumenApp composes MessagePump-based handler mixins before Textual's App.
# Pyright sees MessagePump.is_dom_root (Literal[False]) before App's root
# override even though Textual's runtime MRO/metaclass intentionally supports it.
# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
import sys
from typing import ClassVar, cast
from uuid import uuid4

from pydantic_ai.messages import ModelMessage
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.lazy import Lazy
from textual.widgets import Static
from textual.worker import Worker

from lumen.application import WorkspaceHost
from lumen.approval import ApprovalMode, ApprovalPolicy
from lumen.branding import FRAMEWORK_NAME
from lumen.collaboration import CollaborationMode, apply_collaboration_context
from lumen.config import AppConfig
from lumen.context import ContextSummary
from lumen.events import ApprovalRequest, RunEvent, ToolCallFinished, ToolCallStarted, UsageUpdated
from lumen.interactive_queue import QueueLimitError, QueueMode
from lumen.plan import PlanState
from lumen.resources import ResourceManager
from lumen.run_coordinator import RunInput
from lumen.runtime import ToolApproval
from lumen.sessions import SessionMetadata
from lumen.timeline import TimelineStore
from lumen.tools.capability import build_capability_specs
from lumen.tools.workspace import Workspace
from lumen.ui.activity_indicator import RunActivityIndicator
from lumen.ui.approval_controller import ApprovalControllerMixin
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.autocomplete import CompletionDropdown
from lumen.ui.checkpoint_screen import CheckpointScreen
from lumen.ui.child_run_screen import ChildRunScreen
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
from lumen.ui.history_screen import HistorySearchScreen
from lumen.ui.host_session import HostSessionAdapter
from lumen.ui.plan_panel import PlanPanel
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.queue_panel import InteractiveQueuePanel
from lumen.ui.scroll_follow import ScrollFollowMixin
from lumen.ui.session_restore import SessionRestoreMixin
from lumen.ui.slash_handlers import SlashHandlersMixin
from lumen.ui.status_bar import StatusBarMixin
from lumen.ui.streaming_markdown import AssistantMarkdown, StreamingMarkdownController
from lumen.ui.themes import FALLBACK_COLORS, register_themes, theme_color
from lumen.ui.tool_card import ToolCard
from lumen.ui.transcript_blocks import CommentaryBlock, ReadToolGroup
from lumen.ui.transcript_screen import TranscriptScreen
from lumen.ui.welcome import WelcomePanel

_PROMPT_KEYWORD = re.compile(r"(?<!\S)(@[\w./-]+|/[a-z][\w:-]*|(?:[\w.-]+/)+[\w.-]+)")


def _highlight_prompt(
    value: str,
    *,
    command_color: str = FALLBACK_COLORS["activity"],
    path_color: str = FALLBACK_COLORS["mode-edit"],
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
        Binding("ctrl+o", "toggle_transcript_density", "Transcript density", show=False),
        Binding("ctrl+t", "show_transcript", "Transcript", show=False),
        Binding("ctrl+r", "search_history", "History search", show=False),
        Binding("ctrl+b", "show_children", "Agents", show=False),
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
    /* Error message: same quiet chrome as a system message, but in $error
       so failures never read as routine metadata. */
    .error-message {
        margin: 1 0;
        padding: 0 1;
        color: $error;
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
    /* Progress block: accent-coloured, distinct from commentary. The ↳
       prefix is added in code. */
    .progress-block {
        margin: 1 0;
        padding: 0 1;
        color: $accent;
    }
    .compaction-row { margin: 1 0; color: $text-muted; }
    .compaction-row.is-error { color: $error; }
    .restored-notice {
        margin: 1 0;
        padding: 0 1;
        color: $success;
    }
    .welcome-panel {
        width: 1fr;
        height: 1fr;
        min-height: 12;
        padding: 0 1;
        content-align: center middle;
        text-align: center;
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

    /* Toasts default to the bottom-right corner (Textual's ToastRack), where
       they cover the composer and status line. Dock them top-right below the
       topbar instead; the timeline underneath is scrollable history, so a
       transient overlay there hides nothing interactive. */
    ToastRack {
        dock: top;
        align: right top;
        margin: 1 1 0 0;
    }
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
        self._collaboration_mode = CollaborationMode(config.collaboration.default_mode)
        self._approval_policy = ApprovalPolicy()
        self._assistant_stream: StreamingMarkdownController | None = None
        # One widget represents one logical assistant Markdown document.
        self._assistant_container: AssistantMarkdown | None = None
        self._commentary_container: CommentaryBlock | None = None
        self._thinking_container: CommentaryBlock | None = None
        # The active plan belongs to the current user turn and lives inside
        # the scrolling transcript. Older turns retain their own plan panel.
        self._active_plan_panel: PlanPanel | None = None
        self._last_assistant_output = ""
        # Session rules stay TUI-local; cross-session "always" rules are owned
        # by the host's project ApprovalRuleStore, which auto-approves before
        # an approval event ever reaches this UI.
        self._session_approval_keys: set[str] = set()
        # Tool cards are mounted into the message timeline, keyed by call id so
        # multiple updates to one call render into a single card.
        self._tool_cards: dict[str, ToolCard] = {}
        self._read_tool_groups: dict[str, ReadToolGroup] = {}
        self._current_read_group: ReadToolGroup | None = None
        # Pending approval futures: one per call id. The runtime approval
        # callback awaits the future; the ApprovalPanel's Decision message
        # resolves it.
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
        self.workspace_host = WorkspaceHost(resources)
        # Compatibility-shaped view for UI state restoration; execution,
        # queueing, cancellation, modes, and approvals all cross Host commands.
        self.coordinator = HostSessionAdapter(self.workspace_host)
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
        self._plan_review_revision: int | None = None
        self._transcript_density = config.ui.transcript_density

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
        yield PromptEditor(id="prompt", language=None, placeholder="Ask Lumen…")
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
            editor.configure_vim(self.config.ui.vim_mode)
            await self.workspace_host.open()
            if self.resume_id:
                state = await self.coordinator.resume(self.resume_id)
                restored = True
            else:
                state = await self.coordinator.new_session()
                restored = False
            await self._apply_coordinator_state(state, restored=restored)
            self._refresh_topbar()
            # Initialise the status bar with the real model id + mode suffix
            # (the compose placeholder shows generic text until this runs).
            status = self.query_one("#status", Static)
            self._refresh_mode_classes(status)
            status.update(self._status("Ready"))
            self.query_one(RunActivityIndicator).set_animation_enabled(self.config.ui.animations)
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
        await self.workspace_host.close()

    async def _append_system(self, text: str) -> None:
        container = self.query_one("#messages", VerticalScroll)
        await self._dismiss_welcome()
        follow = self._capture_timeline_follow(container)
        # markup=False is critical: system messages often contain tool/MCP
        # output with ``key: value`` or ``[bracket]`` patterns that Textual's
        # rich-markup parser would misread as style tags and crash on.
        await container.mount(Static(text, classes="system-message", markup=False))
        await self._finish_timeline_update(container, follow)

    async def _append_error(self, text: str) -> None:
        """Mount a failure notice with error-level color, not muted metadata.

        Same markup-safety rationale as :meth:`_append_system`: the failure
        text comes from the provider/runtime and can contain markup-like
        patterns. The ✗ prefix matches the tool-card error glyph vocabulary.
        """

        container = self.query_one("#messages", VerticalScroll)
        await self._dismiss_welcome()
        follow = self._capture_timeline_follow(container)
        await container.mount(Static(f"✗ {text}", classes="error-message", markup=False))
        await self._finish_timeline_update(container, follow)

    async def _append_user(self, text: str) -> None:
        container = self.query_one("#messages", VerticalScroll)
        await self._dismiss_welcome()
        follow = self._capture_timeline_follow(container)
        # Prefix with » to visually mark user input. markup=False is critical:
        # user text can contain anything (paths, code, colons). The » is a
        # safe ASCII-range character that renders in all terminals.
        app = cast(App[object], self)
        rendered = _highlight_prompt(
            text,
            command_color=theme_color(app, "activity", FALLBACK_COLORS["activity"]),
            path_color=theme_color(app, "mode-edit", FALLBACK_COLORS["mode-edit"]),
        )
        await container.mount(Lazy(Static(rendered, classes="user-message", markup=False)))
        await self._finish_timeline_update(container, follow)

    async def _append_commentary(self, text: str) -> None:
        if not text.strip():
            return
        container = self.query_one("#messages", VerticalScroll)
        await self._dismiss_welcome()
        follow = self._capture_timeline_follow(container)
        if self._commentary_container is None:
            self._commentary_container = CommentaryBlock(
                expanded=self._transcript_density == "verbose"
            )
            await container.mount(Lazy(self._commentary_container))
        self._commentary_container.append(text)
        await self._finish_timeline_update(container, follow)

    def _close_commentary_segment(self) -> None:
        self._commentary_container = None

    async def _append_thinking(self, text: str) -> None:
        if not text.strip():
            return
        container = self.query_one("#messages", VerticalScroll)
        await self._dismiss_welcome()
        follow = self._capture_timeline_follow(container)
        if self._thinking_container is None:
            self._thinking_container = CommentaryBlock(
                expanded=self._transcript_density == "verbose",
                label="Model reasoning",
            )
            await container.mount(Lazy(self._thinking_container))
        self._thinking_container.append(text)
        await self._finish_timeline_update(container, follow)

    def _close_thinking_segment(self) -> None:
        self._thinking_container = None

    async def _dismiss_welcome(self) -> None:
        """Remove the one-shot empty state before mounting timeline content."""

        try:
            welcome = self.query_one("#welcome", WelcomePanel)
        except Exception:
            return
        await welcome.remove()

    async def _append_progress(self, summary: str, next_action: str | None) -> None:
        # Prefix with ↳ to distinguish progress from commentary and assistant
        # text. The next-action line keeps its → arrow for the sub-bullet.
        body = f"↳ {summary}" + (f"\n\n→ {next_action}" if next_action else "")
        container = self.query_one("#messages", VerticalScroll)
        await self._dismiss_welcome()
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
        if text.startswith("!"):
            await self._handle_shell_input(text)
            return
        if (
            self._plan_review_revision is not None
            and self._collaboration_mode is CollaborationMode.PLAN
            and not self._run_is_active()
        ):
            self.last_prompt = text
            self._record_history(text)
            await self._append_user(text)
            self.current_worker = self.run_worker(
                self._run_rejected_plan(text),
                name="rejected-plan-run",
                exclusive=True,
            )
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

    async def _handle_shell_input(self, text: str) -> None:
        """Execute explicit ``! argv`` input through the same fail-closed OS sandbox."""

        if self._collaboration_mode is CollaborationMode.PLAN:
            await self._append_system("Direct shell is disabled in Plan mode.")
            return
        if self._run_is_active():
            await self._append_system("Direct shell is unavailable while an agent run is active.")
            return
        try:
            argv = shlex.split(text[1:].strip())
        except ValueError as error:
            await self._append_system(f"Invalid shell command: {error}")
            return
        if not argv:
            await self._append_system("Usage: ! <command> [args…]")
            return
        self._record_history(text)
        await self._append_user(text)
        self.current_worker = self.run_worker(
            self._run_direct_command(argv), name="direct-shell", exclusive=True
        )

    async def _run_direct_command(self, argv: list[str]) -> None:
        call_id = f"shell-{uuid4().hex[:12]}"
        started = asyncio.get_running_loop().time()
        await self.render_event(
            ToolCallStarted(
                call_id,
                "run_command",
                {"argv": argv, "cwd": "."},
                origin="user",
                risk="execute",
                started_at=started,
            )
        )
        specs = build_capability_specs(
            self.resources.workspace,
            max_timeout=self.config.agent.limits.tool_timeout_seconds,
            sandbox_config=self.config.sandbox,
        )
        command = next(spec.function for spec in specs if spec.name == "run_command")
        try:
            result = await command(argv)  # type: ignore[misc]
            rendered = json.dumps(result, ensure_ascii=False, indent=2)
            is_error = bool(result.get("exit_code"))
            exit_code = result.get("exit_code")
            elapsed = float(result.get("elapsed_seconds", 0.0))
        except Exception as error:
            rendered = f"{type(error).__name__}: {error}"
            is_error = True
            exit_code = None
            elapsed = max(0.0, asyncio.get_running_loop().time() - started)
        await self.render_event(
            ToolCallFinished(
                call_id,
                "run_command",
                rendered,
                preview=rendered,
                is_error=is_error,
                elapsed_seconds=elapsed,
                exit_code=exit_code,
            )
        )
        self.query_one(RunActivityIndicator).stop()
        self.query_one("#status", Static).update(self._status("Ready"))

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
            await self.coordinator.set_modes(
                self._approval_mode.value,
                self._collaboration_mode.value,
            )
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

    async def _run_approved_plan(self) -> None:
        revision = self._plan_review_revision
        if revision is None:
            await self._append_system("No plan revision is pending review.")
            return

        async def emit(event: RunEvent) -> None:
            await self.render_event(event)

        outcome = await self.coordinator.approve_plan(
            revision,
            self._approval_mode.value,
            emit,
            self._await_inline_approval,
            self._await_inline_approval_batch,
        )
        self._plan_review_revision = None
        if outcome is None:
            self._resolve_all_pending_approvals(approved=False, message="run failed")
        await self._apply_coordinator_state(self.coordinator.state)
        self._refresh_topbar()

    async def _run_rejected_plan(self, feedback: str) -> None:
        revision = self._plan_review_revision
        if revision is None:
            return

        async def emit(event: RunEvent) -> None:
            await self.render_event(event)

        await self.coordinator.reject_plan(
            revision,
            feedback,
            emit,
            self._await_inline_approval,
            self._await_inline_approval_batch,
        )
        await self._apply_coordinator_state(self.coordinator.state)

    def _apply_permission_mode_context(self, prompt: str) -> str:
        """Apply the orthogonal collaboration mode to the model prompt."""

        return apply_collaboration_context(prompt, self._collaboration_mode)

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
        self._copy_text_to_clipboard(text)
        self.notify("Copied latest response", timeout=2)

    @on(events.TextSelected)
    def copy_mouse_selection(self) -> None:
        """Copy a completed mouse drag selection, including rendered Markdown."""

        selected = self.screen.get_selected_text()
        if not selected:
            return
        self._copy_text_to_clipboard(selected)
        if self.config.ui.notifications:
            self.notify("Selection copied", timeout=1)

    def _copy_text_to_clipboard(self, text: str) -> None:
        """Use Textual's clipboard path plus pbcopy for Apple Terminal."""

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

    def action_toggle_transcript_density(self) -> None:
        """Switch between the concise default and the full audit transcript."""

        self._transcript_density = (
            "verbose" if self._transcript_density == "normal" else "normal"
        )
        expanded = self._transcript_density == "verbose"
        for block in self.query(CommentaryBlock).results(CommentaryBlock):
            block.set_expanded(expanded)
        for card in self.query(ToolCard).results(ToolCard):
            card.set_density(self._transcript_density)
        for group in self.query(ReadToolGroup).results(ReadToolGroup):
            group.set_expanded(expanded)
        self.notify(f"Transcript: {self._transcript_density}", timeout=2)
        if self.session is not None:
            self.run_worker(
                self.coordinator.set_transcript_density(self._transcript_density),
                name="persist-transcript-density",
                exclusive=False,
            )

    def action_show_transcript(self) -> None:
        self.push_screen(
            TranscriptScreen(
                self.timeline_store.items,
                raw=self._transcript_density == "verbose",
            )
        )

    def action_search_history(self) -> None:
        def restore(value: str | None) -> None:
            if value is None:
                return
            editor = self.query_one("#prompt", PromptEditor)
            editor.restore_text(value)
            editor.focus()

        self.push_screen(HistorySearchScreen(self.prompt_history), restore)

    def action_show_children(self) -> None:
        self.push_screen(
            ChildRunScreen(
                self.coordinator.list_child_runs,
                self.coordinator.cancel_child_run,
                message_provider=self.coordinator.send_agent_message,
                followup_provider=self.coordinator.continue_agent,
                import_provider=self.coordinator.approve_agent_import,
                reject_provider=self.coordinator.reject_agent_import,
                close_provider=self.coordinator.close_agent,
                work_state_provider=self.coordinator.work_state,
                waiver_provider=self.coordinator.waive_effect,
            )
        )

    def action_show_checkpoints(self) -> None:
        def resume_branch(session_id: str | None) -> None:
            if session_id is not None:
                self.run_worker(
                    self._resume_checkpoint_branch(session_id),
                    name="resume-checkpoint-branch",
                    exclusive=True,
                )

        self.push_screen(
            CheckpointScreen(
                self.coordinator.list_checkpoints,
                self.coordinator.fork_at_checkpoint,
            ),
            resume_branch,
        )

    async def _resume_checkpoint_branch(self, session_id: str) -> None:
        state = await self.coordinator.resume(session_id)
        await self._apply_coordinator_state(state, restored=True)
        await self._append_system(
            f"Rewound context into new session {session_id}. Workspace files were left unchanged."
        )
        self._refresh_topbar()

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

    @property
    def collaboration_mode(self) -> str:
        return self._collaboration_mode.value

    def set_approval_mode(self, mode: str) -> None:
        """Switch the session permission mode immediately."""

        if mode == "plan":
            self._collaboration_mode = CollaborationMode.PLAN
            self._refresh_topbar()
            if self.session is not None and not self._run_is_active():
                self.run_worker(
                    self.coordinator.set_collaboration("plan"),
                    name="persist-collaboration-mode",
                    exclusive=False,
                )
            try:
                status = self.query_one("#status", Static)
                self._refresh_mode_classes(status)
                status.update(self._status("Ready"))
            except Exception:
                pass
            return
        try:
            parsed = ApprovalMode.parse(mode)
        except ValueError as error:
            raise ValueError(
                f"approval mode must be 'manual', 'accept_edits', or 'auto', got {mode!r}"
            ) from error
        if parsed is self._approval_mode:
            self._collaboration_mode = CollaborationMode.DEFAULT
        else:
            self._approval_mode = parsed
            self._collaboration_mode = CollaborationMode.DEFAULT
        try:
            self.query_one(PlanReviewPanel).hide()
        except Exception:
            pass
        self._refresh_topbar()
        if self.session is not None and not self._run_is_active():
            self.run_worker(
                self.coordinator.set_modes(
                    self._approval_mode.value, self._collaboration_mode.value
                ),
                name="persist-session-modes",
                exclusive=False,
            )
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

        self.set_approval_mode(mode)

    _request_approval_mode = request_approval_mode

    def action_toggle_approval_mode(self) -> None:
        """Cycle modes in the same order as Claude Code's CLI."""

        modes = ("manual", "accept_edits", "plan", "auto")
        current = "plan" if self._collaboration_mode is CollaborationMode.PLAN else self._approval_mode.value
        index = modes.index(current)
        self._request_approval_mode(modes[(index + 1) % len(modes)])

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
