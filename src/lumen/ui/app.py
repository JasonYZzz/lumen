from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any, ClassVar, cast

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
from lumen.branding import FRAMEWORK_NAME, product_label
from lumen.config import AppConfig
from lumen.context import (
    ContextCompactCommand,
    ContextControlResult,
    ContextEngine,
    ContextMemoryCommand,
    ContextReportCommand,
    ContextSummary,
)
from lumen.events import (
    ApprovalRequest,
    CommentaryDelta,
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    InputDelivered,
    InputDequeued,
    InputQueued,
    PlanCreated,
    PlanUpdated,
    ProgressReported,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    TextDelta,
    TextRetracted,
    ToolApprovalPending,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from lumen.interactive_queue import QueueLimitError, QueueMode
from lumen.plan import PlanState
from lumen.resources import ResourceManager
from lumen.run_coordinator import CoordinatorState, RunCoordinator, RunInput
from lumen.runtime import ToolApproval
from lumen.sessions import SessionMetadata
from lumen.skills import expand_skill_for_message
from lumen.timeline import (
    RepositoryTimelineAdapter,
    TimelineItem,
    TimelineKind,
    TimelineStore,
)
from lumen.tools.workspace import Workspace
from lumen.ui.activity_indicator import RunActivityIndicator
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.autocomplete import CompletionDropdown, CompletionSuggestion
from lumen.ui.command_gate import (
    CommandPolicy,
    classify_command,
    classify_model_command,
)
from lumen.ui.commands import LumenCommandProvider
from lumen.ui.composer import ComposerHistory, PromptEditor
from lumen.ui.file_mention import expand_file_mentions
from lumen.ui.file_search import FileSearchHandle, build_suggestion, search_files
from lumen.ui.mode_confirmation import AutoModeConfirmation
from lumen.ui.plan_panel import PlanPanel
from lumen.ui.queue_panel import InteractiveQueuePanel
from lumen.ui.streaming_markdown import AssistantMarkdown, StreamingMarkdownController
from lumen.ui.themes import register_themes
from lumen.ui.tool_card import ToolCard
from lumen.ui.welcome import WelcomePanel


def _fmt_tokens(n: int) -> str:
    """Compact token count: 1234 → ``1.2k``, 567 → ``567``."""

    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


_PROMPT_KEYWORD = re.compile(r"(?<!\S)(@[\w./-]+|/[a-z][\w:-]*|(?:[\w.-]+/)+[\w.-]+)")


def _highlight_prompt(value: str) -> Text:
    """Emphasize mentions, slash commands, and paths without parsing markup."""

    rendered = Text("» ")
    cursor = 0
    for match in _PROMPT_KEYWORD.finditer(value):
        rendered.append(value[cursor : match.start()])
        rendered.append(match.group(0), style="bold underline")
        cursor = match.end()
    rendered.append(value[cursor:])
    return rendered


class LumenApp(App[None]):
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
        # Ctrl+M cycles manual, accept-edits, and auto. Live and
        # session-scoped — does not touch the YAML config.
        Binding("ctrl+m", "toggle_approval_mode", "Toggle approval mode", show=True),
        Binding(
            "shift+tab",
            "toggle_approval_mode",
            "Cycle approval mode",
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
        height: 2;
        padding: 0 2;
        background: $surface 45%;
        color: $accent;
        text-style: bold;
        border-bottom: solid $primary 40%;
    }

    /* Message timeline: minimal chrome so content carries the structure.
       Generous horizontal padding for readability on wide terminals. */
    #messages {
        height: 1fr;
        padding: 1 2;
        background: $background;
    }

    /* --- Message type styling -------------------------------------------
       Each type uses a left-border accent rule + distinct colour to create
       visual hierarchy without heavy borders. The rules are thin (outer)
       so the timeline reads as a continuous flow, not a stack of boxes.
       This mirrors pi/tui's "deep module" philosophy: the message type is
       the interface, the rendering is the hidden implementation. */

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
       supplies the segment's outer spacing. Markdown's own per-block margins
       handle intra-segment separation. */
    .assistant-block { margin: 0; }
    /* Commentary (intermediate model reasoning): visually de-emphasised.
       It's analysis the user can skim, not the final answer. */
    .commentary-block {
        margin: 1 0;
        padding: 0 1;
        color: $secondary;
        text-style: italic;
        border-left: outer $secondary 50%;
    }
    /* Progress block: accent-coloured, distinct from commentary. The ↳
       prefix is added in code. */
    .progress-block {
        margin: 1 0;
        padding: 0 1;
        color: $accent;
        border-left: outer $accent;
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
        background: $surface 45%;
        border-left: outer $accent;
    }

    /* --- Composer ------------------------------------------------------- */
    /* A single rounded prompt box. Enter submits (documented in the status
       line). The focus ring is the primary affordance: nearly invisible
       when unfocused, clear accent border when focused. */
    #prompt {
        height: 3;
        margin: 0 2 1 2;
        border: round $primary 20%;
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
    /* A thin hairline separates timeline from composer. The status bar
       carries dynamic run-state on the left and static keymap hints on
       the right. Background is surface (lifts slightly from $background). */
    #status {
        height: 2;
        padding: 0 2;
        background: $surface 80%;
        color: $text-muted;
        border-top: solid $primary 40%;
    }
    #status.mode-auto { color: $warning; }
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
        # Approval modes are manual, accept-edits, and auto. Legacy "ask"
        # configuration is normalized to manual.
        # ``external_unknown`` remains approval-gated because it represents a
        # remote capability the operator has not classified. Initialised from
        # config but live-toggled via /mode, Ctrl+M, or Shift+Tab.
        self._approval_mode = ApprovalMode.parse(config.permissions.default_mode)
        self._approval_policy = ApprovalPolicy()
        self._assistant_stream: StreamingMarkdownController | None = None
        # One widget represents one logical assistant Markdown document.
        self._assistant_container: AssistantMarkdown | None = None
        self._commentary_container: Static | None = None
        self._commentary_text = ""
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
        # PlanPanel is pinned at the top of the screen (between the topbar and
        # the scrolling message timeline) so the plan stays visible while tool
        # output scrolls beneath it — matching the opencode/cursor layout the
        # user asked for. It's ``display: none`` until a plan arrives.
        yield PlanPanel()
        yield VerticalScroll(
            WelcomePanel(),
            id="messages",
        )
        yield Static("New activity ↓", id="new-activity", markup=False)
        # Stable run controls live directly above the composer. Activity is a
        # compact animated line; approvals replace it with a queue-aware
        # vertical selector without inserting transient controls in history.
        yield RunActivityIndicator()
        yield ApprovalPanel()
        yield InteractiveQueuePanel()
        # Composer is a single rounded prompt box. Enter submits, so there is
        # no Send button — the focus ring is the only affordance, matching the
        # posting/opencode input style.
        yield PromptEditor(id="prompt", language=None)
        # Status bar carries the run-state on the left (Thinking…, Running…,
        # token summary) and fixed mode/model/keymap context so the
        # user always knows which model and approval mode are active. The
        # suffix is appended by _status() on every update.
        yield Static(
            "◆ manual mode  ·  Ready  │  model  │  Shift+Tab mode · / commands · @ files",
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
        # ``$surface`` / ``$accent`` / etc. tokens resolve against the lumen-dark
        # palette from the first paint. Default theme is set inside
        # ``register_themes``.
        register_themes(cast(App[object], self))
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
            status.set_class(self._approval_mode is ApprovalMode.AUTO, "mode-auto")
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

    def on_resize(self, event: Any) -> None:  # type: ignore[no-untyped-def]
        # Collapse the plan panel on short terminals so it doesn't eat the
        # message timeline. Textual hands us the new size on resize; we don't
        # read escape sequences ourselves. Below 20 rows the plan collapses to
        # a one-line summary so the scrolling execution area stays usable.
        try:
            panel = self.query_one(PlanPanel)
        except Exception:
            return
        panel.collapse(event.size.height < 20)

    def _refresh_topbar(self) -> None:
        session_id = self.session.id[:8] if self.session else "none"
        model_segment = self._model_display()
        # Mode badge: "auto" calls attention to the fact that read-only tools
        # will skip confirmation. We uppercase it to stand out.
        mode_segment = "AUTO" if self._approval_mode is ApprovalMode.AUTO else self._approval_mode.value
        # MCP status: only shown when there are servers AND some are unhealthy.
        # A fully-healthy or zero-MCP config stays silent to reduce noise.
        mcp_segment = self._mcp_warning_segment()
        topbar = (
            f"◆ {product_label(self.config.agent.name)}  ·  model {model_segment}  ·  "
            f"{mode_segment} mode  ·  session {session_id}"
        )
        if mcp_segment:
            topbar += f"  ·  {mcp_segment}"
        self.query_one("#topbar", Static).update(topbar)
        self._refresh_welcome_panel()

    def _model_display(self) -> str:
        model_id = self.resources.active_model_config().id
        available = self.resources.available_models()
        if len(available) <= 1:
            return model_id
        active_name = self.resources.active_model_name()
        index = available.index(active_name) + 1
        return f"{active_name} [{model_id}] {index}/{len(available)}"

    def _refresh_welcome_panel(self) -> None:
        try:
            welcome = self.query_one("#welcome", WelcomePanel)
        except Exception:
            return
        statuses = self.resources.mcp_status
        if statuses:
            ready = sum(status == "ok" for status in statuses.values())
            mcp_summary = f"MCP {ready}/{len(statuses)} ready"
        else:
            mcp_summary = "no MCP servers"
        welcome.update_context(
            agent_name=self.config.agent.name,
            model=self._model_display(),
            mode=self._approval_mode.value,
            session_id=self.session.id[:8] if self.session is not None else "starting",
            workspace=self.resources.workspace,
            tool_count=len(self.resources.tool_metadata),
            skill_count=len(self.resources.skills),
            mcp_summary=mcp_summary,
        )

    def _mcp_warning_segment(self) -> str:
        """MCP status text, shown only when there's a problem.

        Returns ``""`` when MCP is healthy or unconfigured, so the topbar
        stays clean in the common case. When some servers failed, we surface
        a compact warning.
        """

        statuses = self.resources.mcp_status
        if not statuses:
            return ""
        ok = sum(1 for value in statuses.values() if value == "ok")
        if ok == len(statuses):
            return ""  # all healthy — no noise
        return f"MCP {ok}/{len(statuses)}"

    def _mcp_summary(self) -> str:
        statuses = self.resources.mcp_status
        if not statuses:
            return "0"
        ok = sum(1 for value in statuses.values() if value == "ok")
        return f"{ok}/{len(statuses)}"

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
        await container.mount(Lazy(Static(_highlight_prompt(text), classes="user-message", markup=False)))
        await self._finish_timeline_update(container, follow)

    async def _append_commentary(self, text: str) -> None:
        if not text.strip():
            return
        container = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(container)
        if self._commentary_container is None:
            self._commentary_container = Static("", classes="commentary-block", markup=False)
            self._commentary_text = ""
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
        body = f"↳ {summary}" + (f"\n  → {next_action}" if next_action else "")
        container = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(container)
        await container.mount(Lazy(Static(body, classes="progress-block", markup=False)))
        await self._finish_timeline_update(container, follow)

    def _capture_timeline_follow(self, container: VerticalScroll) -> bool:
        """Snapshot tail state before content changes its scroll extent."""

        if self._assistant_stream is not None and self._follow_tail:
            # A render frame may have increased Markdown height before the
            # deferred scroll-to-end callback runs. That transient lag is not
            # evidence that the user intentionally left the tail.
            return True
        target_y = float(container.scroll_target_y)
        # Hiding the activity row or resizing the viewport may reduce
        # ``max_scroll_y`` and clamp the target upward.  Compare against the
        # last reachable tail, not the previous (now impossible) coordinate;
        # otherwise a layout shrink is mistaken for an explicit user scroll.
        reachable_last_tail = min(self._last_tail_scroll_y, float(container.max_scroll_y))
        if self._follow_tail and target_y < reachable_last_tail:
            # A user scroll command updates the target before the animated /
            # deferred position necessarily moves. Treat that as an explicit
            # departure from the tail so a queued refresh cannot pull them
            # back down.
            self._follow_tail = False
        elif container.max_scroll_y <= 0 or container.is_vertical_scroll_end:
            self._follow_tail = True
            self._last_tail_scroll_y = float(container.scroll_y)
        elif self._follow_tail and float(container.scroll_y) >= self._last_tail_scroll_y:
            # Layout can grow after a previous scroll_end (Markdown rendering,
            # Lazy widgets, welcome panel). The viewport then temporarily no
            # longer reports "at end" even though the user never scrolled.
            # Preserve follow mode unless the scroll position actually moved
            # upward from the last programmatic tail position.
            self._follow_tail = True
        else:
            self._follow_tail = False
        return self._follow_tail

    async def _finish_timeline_update(self, container: VerticalScroll, follow_before_update: bool) -> None:
        await self._prune_timeline_widgets(container)
        try:
            indicator = self.query_one("#new-activity", Static)
        except Exception:
            return
        if follow_before_update:
            self._scroll_timeline_end(container)
            indicator.remove_class("visible")
        else:
            indicator.add_class("visible")

    def _scroll_timeline_end(self, container: VerticalScroll) -> None:
        """Follow the tail now and again after Textual's next layout pass."""

        self._tail_follow_generation += 1
        generation = self._tail_follow_generation
        # Textual's native anchor tracks later Rich/Markdown layout growth;
        # repeated scroll_end callbacks alone can run before final measurement.
        container.anchor(True)
        container.scroll_end(animate=False)
        self._last_tail_scroll_y = max(float(container.scroll_y), float(container.scroll_target_y))

        def after_layout() -> None:
            if generation != self._tail_follow_generation or not self._follow_tail:
                return
            reachable_last_tail = min(self._last_tail_scroll_y, float(container.max_scroll_y))
            if float(container.scroll_target_y) < reachable_last_tail:
                self._follow_tail = False
                return
            container.scroll_end(animate=False, force=True, immediate=True)
            self._last_tail_scroll_y = max(float(container.scroll_y), float(container.scroll_target_y))

            def settle_markdown_layout() -> None:
                if generation == self._tail_follow_generation and self._follow_tail:
                    container.scroll_end(animate=False, force=True, immediate=True)

            self.call_after_refresh(settle_markdown_layout)

        self.call_after_refresh(after_layout)

    async def _prune_timeline_widgets(self, container: VerticalScroll, *, remove_oldest: bool = True) -> None:
        """Bound mounted widgets while retaining every pending approval."""

        allowed = 202 + len(self._approval_waiters)
        while len(container.children) > allowed:
            candidates = container.children if remove_oldest else reversed(container.children)
            removable = next(
                (
                    child
                    for child in candidates
                    if child is not self._assistant_container
                    and not (isinstance(child, ToolCard) and child.call_id in self._approval_waiters)
                ),
                None,
            )
            if removable is None:
                break
            if isinstance(removable, ToolCard):
                self._tool_cards.pop(removable.call_id, None)
            await removable.remove()

        completed_cards = [
            child
            for child in container.children
            if isinstance(child, ToolCard) and child.call_id not in self._approval_waiters
        ]
        for card in completed_cards[:-20]:
            card.compact()

    async def _load_older_if_at_top(self) -> None:
        """Page older turns when a restored timeline reaches its top."""

        if self._loading_older or self.timeline_store.next_cursor is None:
            return
        messages = self.query_one("#messages", VerticalScroll)
        if messages.max_scroll_y <= 0 or messages.scroll_y > 0:
            return
        self._loading_older = True
        try:
            anchor = messages.children[0] if messages.children else None
            anchor_y = anchor.virtual_region.y if anchor is not None else 0
            page = self.timeline_store.load_older(self.timeline_store.next_cursor, limit=20)
            widgets = [self._timeline_widget(item) for item in page.items]
            widgets = [widget for widget in widgets if widget is not None]
            if widgets:
                if anchor is None:
                    await messages.mount(*widgets)
                else:
                    await messages.mount(*widgets, before=anchor)
                    await self._prune_timeline_widgets(messages, remove_oldest=False)

                    def restore_anchor() -> None:
                        delta = anchor.virtual_region.y - anchor_y
                        messages.scroll_relative(y=delta, animate=False)

                    self.call_after_refresh(restore_anchor)
        finally:
            self._loading_older = False

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
    ) -> None:
        if text.startswith("/"):
            await self._handle_command(text)
            return
        if self.current_worker is not None and self.current_worker.is_running:
            expanded = expand_file_mentions(
                model_prompt if model_prompt is not None else text,
                Workspace(self.resources.workspace),
            )
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
            self._run_prompt(RunInput(display_text=text, model_prompt=expanded)),
            name="agent-run",
            exclusive=True,
        )

    def _record_history(self, text: str) -> None:
        """Append ``text`` to prompt history, capped at ``_HISTORY_MAX``.

        We deduplicate against the most recent entry so pressing Enter twice
        on the same prompt doesn't flood the history.
        """

        self._composer_history.record(text)

    # -- completion dropdown wiring ----------------------------------------

    @on(PromptEditor.CompletionRequested)
    async def _on_completion_requested(self, event: PromptEditor.CompletionRequested) -> None:
        await self._refresh_completions(event.prefix)

    @on(PromptEditor.HistoryNavigation)
    def _on_history_navigation(self, event: PromptEditor.HistoryNavigation) -> None:
        editor = self.query_one("#prompt", PromptEditor)
        result = self._composer_history.navigate(editor.text, event.direction)
        if result is None:
            return
        editor.text = result.text
        # Keep the cursor at the start so repeated Up/Down presses keep
        # walking the stack. The user can press Right / End / click to edit
        # the recalled prompt; once they do, history navigation stops because
        # the cursor is no longer at (0, 0).
        editor.cursor_location = (0, 0) if result.browsing else editor.document.end

    @on(CompletionDropdown.SuggestionSelected)
    def _on_suggestion_selected(self, event: CompletionDropdown.SuggestionSelected) -> None:
        editor = self.query_one("#prompt", PromptEditor)
        # Set the suppress guard so the text mutation from replace() doesn't
        # immediately re-open the dropdown (the inserted text like
        # ``@README.md `` still starts with ``@``).
        editor._suppress_completion = True  # type: ignore[reportPrivateUsage]
        # Replace the trigger token at the cursor with the chosen suggestion's
        # ``insert`` text. The token is whatever the editor reported as the
        # current trigger prefix (e.g. "@src/" or "/mode").
        self._replace_token_at_cursor(editor, event.prefix, event.suggestion.insert)
        editor.focus()
        dropdown = self.query_one(CompletionDropdown)
        dropdown.hide()

    @on(CompletionDropdown.Dismissed)
    def _on_completion_dismissed(self, event: CompletionDropdown.Dismissed) -> None:
        self.query_one(CompletionDropdown).hide()
        self.query_one("#prompt", PromptEditor).focus()

    @staticmethod
    def _replace_token_at_cursor(editor: PromptEditor, token: str, replacement: str) -> None:
        """Replace the trigger ``token`` at the cursor with ``replacement``.

        ``token`` is the prefix the editor extracted (e.g. ``"@src"``); we
        overwrite the slice [col-len(token), col) on the current row with
        ``replacement``. If the editor's text drifted (user kept typing after
        the dropdown opened), we fall back to inserting at the cursor.
        """

        if not token:
            return
        row, col = editor.cursor_location
        line = editor.document.get_line(row)
        start = col - len(token)
        if start < 0 or line[start:col] != token:
            editor.insert(replacement)
            return
        # ``replace`` takes (start, end) locations as (row, col) tuples.
        editor.replace(replacement, start=(row, start), end=(row, col))

    async def _refresh_completions(self, prefix: str) -> None:
        """Populate the dropdown based on the trigger prefix.

        ``prefix`` is the editor's current trigger token:
        - ``"@…"`` → tree-wide file search under the workspace root
        - ``"/"`` (and only at the very start of the input) → slash commands
        - ``""`` → no completion; hide the dropdown

        After populating file suggestions we reposition the dropdown above the
        prompt editor so it never overlaps the input box.
        """

        self._completion_generation += 1
        generation = self._completion_generation
        if self._completion_search_handle is not None:
            self._completion_search_handle.cancel()
            self._completion_search_handle = None
        dropdown = self.query_one(CompletionDropdown)
        if not prefix:
            dropdown.hide()
            return

        if prefix.startswith("@"):
            # Tree-wide search: ``@app`` finds ``src/lumen/ui/app.py``
            # without making the user descend directory by directory.
            # search_files shells out to fd (or falls back to os.walk) which
            # is blocking I/O — run it in a thread so the UI stays responsive.
            # Without this the TUI freezes for up to 2s per ``@`` keystroke.
            await asyncio.sleep(0.075)
            if generation != self._completion_generation:
                return
            handle = FileSearchHandle(
                prefix,
                self.resources.registry.workspace,
                search=search_files,
            )
            self._completion_search_handle = handle
            try:
                hits = await asyncio.to_thread(handle.run)
            finally:
                if self._completion_search_handle is handle:
                    self._completion_search_handle = None
            if generation != self._completion_generation:
                return
            suggestions = [build_suggestion(h, prefix) for h in hits]
            if not suggestions:
                # Show an explicit "no matches" state instead of silently
                # hiding the dropdown — the user needs feedback that the
                # search ran and found nothing.
                dropdown.set_suggestions(
                    [CompletionSuggestion(label="(no files found)", insert=prefix)],
                    prefix=prefix,
                )
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
                return
            dropdown.set_suggestions(suggestions, prefix=prefix)
            if suggestions:
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
            return

        if prefix.startswith("/"):
            suggestions = self._slash_command_suggestions(prefix)
            if not suggestions:
                dropdown.set_suggestions(
                    [CompletionSuggestion(label="(no matching commands)", insert=prefix)],
                    prefix=prefix,
                )
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
                return
            dropdown.set_suggestions(suggestions, prefix=prefix)
            if suggestions:
                editor = self.query_one("#prompt", PromptEditor)
                dropdown.anchor_above(editor)
            return

        dropdown.hide()

    def _slash_command_suggestions(self, prefix: str) -> list[CompletionSuggestion]:
        """List the built-in slash commands matching ``prefix``.

        Includes dynamic ``/skill:<name>`` entries for every discovered skill,
        so the user can discover and invoke skills from the completion
        dropdown just like builtin commands.
        """

        commands = [
            ("/help", "Show command reference"),
            ("/clear", "Clear the visible timeline · keep session context"),
            ("/new", "Start a fresh session"),
            ("/model", f"Switch model · current: {self._model_display()}"),
            ("/mode", f"Approval policy · current: {self._approval_mode}"),
            ("/sessions", "List past sessions"),
            ("/resume", "Resume a session by id"),
            ("/tools", f"List visible tools · {len(self.resources.tool_metadata)} loaded"),
            ("/skills", f"List available skills · {len(self.resources.skills)} loaded"),
            ("/context", "Show context budget · zones, blocks, pressure"),
            ("/compact", "Force a compaction (M4)"),
            ("/memory", "Memory commands (M5)"),
            ("/retry", "Re-send the last prompt"),
            ("/quit", f"Exit {FRAMEWORK_NAME}"),
        ]
        # Dynamic /skill:<name> entries — one per discovered skill, with the
        # skill description truncated for dropdown readability.
        for skill in self.resources.skills:
            commands.append((f"/skill:{skill.name}", skill.description[:60]))
        frag = prefix  # prefix already starts with "/"
        return [
            CompletionSuggestion(label=cmd, insert=cmd + " ", description=desc)
            for cmd, desc in commands
            if cmd.startswith(frag)
        ]

    def _run_is_active(self) -> bool:
        """Whether an agent-run worker is currently executing."""
        return self.current_worker is not None and self.current_worker.is_running

    async def _cancel_active_run(self) -> None:
        """Request cancellation of the active run worker, if any.

        Used by the command gate before /quit so the run tears down cleanly
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

    def _context_engine(self) -> ContextEngine | None:
        """The active session's context engine, if a runtime is open."""

        runtime = self.resources.runtime
        return runtime.context_engine if runtime is not None else None

    async def _run_context_control(self, command: Any) -> None:
        """Run a read-only/stub context control command and display its result."""

        engine = self._context_engine()
        if engine is None:
            await self._append_system("Context engine is not available yet.")
            return

        async def _noop(_event: Any) -> None:
            return None

        result = await engine.control(command, _noop)
        await self._append_system(self._format_context_result(result))

    async def _render_context(self, parts: list[str]) -> None:
        """Render the ``/context`` budget report (``--json`` for machine output)."""

        engine = self._context_engine()
        if engine is None:
            await self._append_system("Context engine is not available yet.")
            return

        async def _noop(_event: Any) -> None:
            return None

        result = await engine.control(ContextReportCommand(), _noop)
        if len(parts) > 1 and parts[1] == "--json":
            await self._append_system(json.dumps(result.payload, ensure_ascii=False, indent=2))
            return
        await self._append_system(self._format_context_result(result))

    @staticmethod
    def _format_context_result(result: ContextControlResult) -> str:
        """Format a control result as the ``/context`` zone table (plan §14.1)."""

        payload = result.payload
        if not payload:
            return result.message or "No context prepared yet."
        lines = [result.message]
        for zone in payload.get("zones", []):
            share_pct = zone["share"] * 100
            lines.append(f"  {zone['zone']:<18} {zone['tokens']:>7}  {share_pct:5.1f}%  {zone['survival']}")
        pressure = payload.get("pressure", [])
        if pressure:
            lines.append("Top pressure:")
            for item in pressure[:5]:
                lines.append(f"  {item['label']}: {item['tokens']}  ({item['source']})")
        if payload.get("estimated"):
            lines.append("(model window estimated; no known profile for this provider)")
        return "\n".join(lines)

    async def _handle_command(self, line: str) -> None:
        try:
            parts = shlex.split(line)
        except ValueError as error:
            await self._append_system(f"Invalid command: {error}")
            return
        command = parts[0].lower()
        # Gate state-destroying / run-starting commands while an agent run is
        # active. Read-only commands (/help, /mode, /tools, /skills, /sessions)
        # always pass; /new, /resume, /model <name> are refused; /retry and
        # /skill:* are refused (they start a new run); /quit cancels first.
        if self._run_is_active():
            policy = classify_command(line)
            if command == "/model":
                policy = classify_model_command(parts)
            if policy is CommandPolicy.BLOCK:
                await self._append_system(
                    f"{command} is disabled while a run is active. Press Esc to cancel the run first."
                )
                return
            if policy is CommandPolicy.QUEUE:
                await self._append_system(
                    f"{command} starts a new run and can't be used while one is active. "
                    "Press Esc to cancel first."
                )
                return
            if policy is CommandPolicy.CANCEL_THEN_RUN:
                await self._cancel_active_run()
        # /skill:<name> [args] — manually trigger a skill. The skill body is
        # expanded into a <skill> XML block and sent as a user message, so
        # the model receives the full instructions in-context. This works
        # even for skills with disable-model-invocation: true.
        if command.startswith("/skill:"):
            skill_name = command[len("/skill:") :]
            skill = self.resources.load_skill_by_name(skill_name)
            if skill is None:
                await self._append_system(
                    f"Unknown skill: {skill_name}. Use /skills to list available skills."
                )
                return
            args = shlex.join(parts[1:]) if len(parts) > 1 else ""
            # Persist the exact user input. Skill instructions are model-only,
            # so timeline browsing and /retry never expose expanded XML.
            display = line
            await self._append_user(display)
            expanded = expand_skill_for_message(skill, args)
            expanded_prompt = expand_file_mentions(expanded, Workspace(self.resources.workspace))
            self.current_worker = self.run_worker(
                self._run_prompt(RunInput(display_text=display, model_prompt=expanded_prompt)),
                name="agent-run",
                exclusive=True,
            )
            return
        if command == "/skills":
            if not self.resources.skills:
                await self._append_system(
                    "No skills found. Add SKILL.md files to .lumen/skills/ or ~/.lumen/skills/."
                )
                return
            rows = [f"  {s.name}  —  {s.description[:80]}" for s in self.resources.skills]
            await self._append_system(f"{len(self.resources.skills)} skill(s) available:\n" + "\n".join(rows))
            return
        if command == "/help":
            await self._append_system(
                "/help · /clear · /new · /model [name] · "
                "/mode [manual|accept_edits|auto] · /sessions · "
                "/resume <id> · /tools · /skills · /skill:<name> · "
                "/context [--json] · /compact · /memory · /retry · /quit"
            )
        elif command == "/clear":
            await self._clear_visible_timeline()
        elif command == "/new":
            # A new session must NOT inherit the previous session's compaction
            # summary or its compaction row — those belong to the old session.
            # Clearing here is the isolation boundary: no App-level state
            # crosses the /new seam.
            self._clear_compaction_row()
            await self._apply_coordinator_state(self.coordinator.new_session())
            assert self.session is not None
            await self._append_system(f"Started new session {self.session.id}")
            self._refresh_topbar()
        elif command == "/model" and len(parts) == 1:
            # List configured models with the active one marked.
            available = self.resources.available_models()
            active = self.resources.active_model_name()
            rows: list[str] = []
            for name in available:
                model_cfg = self.resources.model_registry[name]
                marker = "* " if name == active else "  "
                rows.append(f"{marker}{name}  ->  {model_cfg.id}")
            await self._append_system("\n".join(rows) or "No models configured.")
        elif command == "/model" and len(parts) == 2:
            target = parts[1]
            if target not in self.resources.model_registry:
                await self._append_system(
                    f"Unknown model {target!r}. Available: {self.resources.available_models()}"
                )
                return
            if target == self.resources.active_model_name():
                await self._append_system(f"Already on {target}.")
                return
            try:
                await self.resources.select_model(target)
            except Exception as error:
                await self._append_system(f"Failed to switch model: {error}")
                return
            self._refresh_topbar()
            self.query_one("#status", Static).update(self._status("Ready"))
            await self._append_system(f"Switched to {target} ({self.resources.active_model_config().id}).")
        elif command == "/mode" and len(parts) == 1:
            # Report the current mode and what it means, so the user knows what
            # they're toggling without having to read the README.
            current = self._approval_mode
            behaviour = (
                "auto-approving classified tools (unknown remote tools still require confirmation)"
                if current is ApprovalMode.AUTO
                else (
                    "auto-approving builtin file edits; confirming commands and remote writes"
                    if current is ApprovalMode.ACCEPT_EDITS
                    else "confirming every risky tool call"
                )
            )
            await self._append_system(
                f"Approval mode: {current.value} ({behaviour}). Press Shift+Tab to cycle, or use "
                "/mode manual|accept_edits|auto."
            )
        elif command == "/mode" and len(parts) == 2:
            target_mode = parts[1].lower()
            if target_mode not in {"ask", "manual", "accept_edits", "auto"}:
                await self._append_system(
                    f"Unknown mode {parts[1]!r}. Use 'manual', 'accept_edits', or 'auto'."
                )
                return
            self._request_approval_mode(target_mode)
        elif command in {"/sessions", "/resume"} and len(parts) == 1:
            sessions = self.resources.session_repository.list()
            await self._append_system(
                "\n".join(f"{item.id}  {item.created_at}  {item.model_id}" for item in sessions)
                or "No sessions found."
            )
        elif command == "/resume" and len(parts) == 2:
            try:
                state = self.coordinator.resume(parts[1])
            except Exception as error:
                await self._append_system(f"Cannot resume session: {error}")
            else:
                await self._apply_coordinator_state(state, restored=True)
                await self._append_system(f"Resumed session {state.session.id}")
                self._refresh_topbar()
        elif command == "/tools":
            lines = [
                f"{name}  [{metadata['risk']}]  {metadata['origin']}"
                for name, metadata in sorted(self.resources.tool_metadata.items())
            ]
            await self._append_system("\n".join(lines) or "No tools enabled.")
        elif command == "/context":
            await self._render_context(parts)
        elif command == "/compact":
            focus = shlex.join(parts[1:]) if len(parts) > 1 else None
            await self._run_context_control(ContextCompactCommand(focus=focus))
        elif command == "/memory":
            action = parts[1] if len(parts) > 1 else "list"
            await self._run_context_control(ContextMemoryCommand(action=action))
        elif command == "/retry":
            if self.last_prompt is None:
                await self._append_system("There is no previous prompt to retry.")
            else:
                await self.handle_input(self.last_prompt)
        elif command == "/quit":
            self.exit()
        else:
            await self._append_system(f"Unknown command: {command}. Use /help.")

    async def _clear_visible_timeline(self) -> None:
        """Clear rendered activity without changing the model conversation.

        This mirrors mature coding-agent TUIs: ``/clear`` is a view operation,
        while ``/new`` is the explicit context/session boundary.
        """

        messages = self.query_one("#messages", VerticalScroll)
        for child in list(messages.children):
            await child.remove()
        self._tool_cards.clear()
        self._compaction_row = None
        self._assistant_container = None
        self._assistant_active = None
        self._assistant_frozen_count = 0
        await messages.mount(WelcomePanel())
        self._refresh_welcome_panel()
        self._follow_tail = True
        self.query_one("#new-activity", Static).remove_class("visible")
        self._scroll_timeline_end(messages)

    async def _apply_coordinator_state(self, state: CoordinatorState, *, restored: bool = False) -> None:
        """Copy authoritative coordinator state into transient render state."""

        previous_session_id = self.session.id if self.session is not None else None
        self.session = state.session
        self.history = list(state.history)
        self.plan = state.plan
        self.last_prompt = state.last_user_input
        # Restore THIS session's compaction summary so iterative compaction
        # continues from it — never a stale App-level value carried over from
        # a previous session. Session state switches are atomic: all four
        # fields are set together before any render happens.
        self._last_compaction_summary = state.compaction_summary
        if previous_session_id != state.session.id:
            self.timeline_store = TimelineStore(
                RepositoryTimelineAdapter(self.resources.session_repository, state.session.id)
            )
            if previous_session_id is not None:
                messages = self.query_one("#messages", VerticalScroll)
                for child in list(messages.children):
                    await child.remove()
                self._tool_cards.clear()
                self._compaction_row = None
                if not restored:
                    await messages.mount(WelcomePanel())
        if restored:
            self.timeline_store.load_older(limit=20)
            await self._restore_timeline_widgets()
            self._after_session_load()

    def _after_session_load(self) -> None:
        try:
            panel = self.query_one(PlanPanel)
        except Exception:
            return
        panel.update_plan(self.plan)
        restored_text = self._restored_notice()
        if restored_text:
            messages = self.query_one("#messages", VerticalScroll)
            messages.mount(Lazy(Static(restored_text, classes="restored-notice", markup=False)))

    async def _restore_timeline_widgets(self) -> None:
        messages = self.query_one("#messages", VerticalScroll)
        for child in list(messages.children):
            await child.remove()
        for item in self.timeline_store.window():
            widget = self._timeline_widget(item)
            if widget is not None:
                await messages.mount(widget)
        await self._prune_timeline_widgets(messages)
        self._scroll_timeline_end(messages)

    def _timeline_widget(self, item: TimelineItem) -> Any:
        if item.kind is TimelineKind.USER:
            return Lazy(Static(f"» {item.text}", classes="user-message", markup=False))
        if item.kind is TimelineKind.ASSISTANT:
            return AssistantMarkdown(item.text, classes="assistant-message")
        if item.kind is TimelineKind.COMMENTARY:
            return Lazy(Static(item.text, classes="commentary-block", markup=False))
        if item.kind is TimelineKind.PROGRESS:
            return Lazy(Static(f"↳ {item.text}", classes="progress-block", markup=False))
        if item.kind is TimelineKind.TOOL and item.call_id and item.tool_name:
            card = ToolCard(item.call_id, item.tool_name)
            card.start(args=item.args or {}, origin="session", risk="recorded")
            if item.result is not None:
                card.update_result(
                    result=item.result,
                    preview=item.preview,
                    is_error=item.is_error,
                )
            else:
                card.compact()
            self._tool_cards[item.call_id] = card
            return card
        css = "system-message" if item.kind is not TimelineKind.ERROR else "system-message is-error"
        return Lazy(Static(item.text, classes=css, markup=False))

    def _restored_notice(self) -> str:
        steps = len(self.plan.steps)
        completed = sum(1 for s in self.plan.steps if s.status.value == "completed")
        return (
            f"Resumed session: plan has {completed}/{steps} steps completed, "
            f"{len(self.history)} messages in active context."
        )

    async def _run_prompt(self, run_input: RunInput) -> None:
        if self.resources.runtime is None or self.session is None:
            await self._append_system("Runtime is not available.")
            return

        async def emit(event: RunEvent) -> None:
            await self.render_event(event)

        async def approve(request: ApprovalRequest) -> ToolApproval:
            return await self._await_inline_approval(request)

        try:
            outcome = await self.coordinator.run(run_input, emit, approve)
        except asyncio.CancelledError:
            self._resolve_all_pending_approvals(approved=False, message="run cancelled")
            raise
        else:
            if outcome is None:
                self._resolve_all_pending_approvals(approved=False, message="run failed")
            await self._apply_coordinator_state(self.coordinator.state)
        finally:
            self._refresh_topbar()

    async def _await_inline_approval(self, request: ApprovalRequest) -> ToolApproval:
        """Allocate a future for ``request`` and await the card's decision.

        In ``auto`` mode, every explicitly classified risk skips the card: we
        resolve immediately so the run isn't blocked on a confirmation the
        user opted out of. ``external_unknown`` remains approval-gated because
        the operator has not classified that remote capability.
        """

        policy_decision = self._approval_policy.decide(request, self._approval_mode)
        if policy_decision.approved and not policy_decision.requires_confirmation:
            return ToolApproval(
                approved=True,
                message=policy_decision.message,
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolApproval] = loop.create_future()
        self._approval_waiters[request.call_id] = future
        # Surface the pending request to the user via the matching tool card.
        pending = ToolApprovalPending(
            call_id=request.call_id,
            name=request.name,
            args=request.args,
            origin=request.origin,
            risk=request.risk,
        )
        await self._render_event(pending)
        return await future

    def _should_auto_approve(self, risk: str, *, name: str = "", origin: str = "builtin") -> bool:
        """Whether ``risk`` is auto-approved under the current mode.

        ``manual`` mode never short-circuits — every CONFIRM goes to the panel.
        ``auto`` mode short-circuits every known risk. The sole exception is
        ``external_unknown``, the safe default for an MCP tool the operator has
        not classified. This is the single choke point for local and MCP tools.
        """

        decision = self._approval_policy.decide(
            ApprovalRequest(call_id="policy-check", name=name, args={}, origin=origin, risk=risk),
            self._approval_mode,
        )
        return decision.approved and not decision.requires_confirmation

    def _resolve_all_pending_approvals(self, *, approved: bool, message: str) -> None:
        audit_message = f"{message} (mode={self._approval_mode.value}, decision_source=system)"
        for call_id, future in list(self._approval_waiters.items()):
            if not future.done():
                future.set_result(ToolApproval(approved=approved, message=audit_message))
            card = self._tool_cards.get(call_id)
            if card is not None:
                card.resolve_approval(approved=approved)
        self._approval_waiters.clear()
        try:
            self.query_one(ApprovalPanel).clear()
        except Exception:
            pass

    @on(ToolCard.Decision)
    def _handle_tool_decision(self, event: ToolCard.Decision) -> None:
        future = self._approval_waiters.pop(event.call_id, None)
        if future is None or future.done():
            return
        action = "allowed" if event.approved else "denied"
        message = (
            f"The user {action} this tool call (mode={self._approval_mode.value}, decision_source=user)."
        )
        future.set_result(ToolApproval(approved=event.approved, message=message))
        # Return focus to the prompt editor so the user can keep typing. The
        # tool card grabbed focus when its approval selector mounted; now that
        # the decision is resolved we hand it back.
        self.query_one("#prompt", PromptEditor).focus()

    @on(ApprovalPanel.Decision)
    def _handle_approval_decision(self, event: ApprovalPanel.Decision) -> None:
        panel = self.query_one(ApprovalPanel)
        future = self._approval_waiters.pop(event.call_id, None)
        panel.resolve(event.call_id)
        if future is None or future.done():
            return
        action = "allowed" if event.approved else "denied"
        message = (
            f"The user {action} this tool call (mode={self._approval_mode.value}, decision_source=user)."
        )
        future.set_result(ToolApproval(approved=event.approved, message=message))
        if panel.active_request is None:
            self.query_one("#prompt", PromptEditor).focus()

    async def render_event(self, event: RunEvent) -> None:
        """Public UI-adapter seam for rendering one structured run event."""

        await self._render_event(event)

    async def _render_event(self, event: RunEvent) -> None:
        messages = self.query_one("#messages", VerticalScroll)
        follow = self._capture_timeline_follow(messages)
        self.timeline_store.apply(event)
        status = self.query_one("#status", Static)
        activity = self.query_one(RunActivityIndicator)
        if not isinstance(event, CommentaryDelta):
            self._close_commentary_segment()
        if isinstance(event, InputQueued | InputDelivered | InputDequeued):
            self._refresh_interactive_queue()
        elif isinstance(event, RunStarted):
            await self._close_assistant_segment()
            status.update(self._status("Thinking…"))
            activity.start("Thinking", "understanding the request")
        elif isinstance(event, TextDelta):
            await self._ensure_assistant_segment(messages)
            assert self._assistant_stream is not None
            self._assistant_stream.append(event.text)
            if activity.label != "Writing response":
                activity.describe("Writing response")
        elif isinstance(event, TextRetracted):
            document = self._assistant_container
            await self._close_assistant_segment()
            if document is not None:
                await document.remove()
        elif isinstance(event, CommentaryDelta):
            await self._close_assistant_segment()
            await self._append_commentary(event.text)
            activity.describe("Thinking", "reviewing intermediate results")
        elif isinstance(event, PlanCreated | PlanUpdated):
            self.plan = event.plan
            # PlanPanel is mounted at compose time (pinned at the top), so we
            # can query it directly — no lazy mount into the message timeline.
            panel = self.query_one(PlanPanel)
            panel.update_plan(event.plan)
            active_step = next(
                (step.title for step in event.plan.steps if step.status.value == "in_progress"),
                None,
            )
            activity.describe("Updating todo list", active_step)
        elif isinstance(event, ProgressReported):
            await self._close_assistant_segment()
            await self._append_progress(event.summary, event.next_action)
            activity.describe("Working", event.next_action or event.summary)
        elif isinstance(event, ToolCallStarted):
            # A tool call terminates the current contiguous assistant-text
            # segment. Any later TextDelta gets a new Markdown widget after
            # this card, preserving the actual event order.
            await self._close_assistant_segment()
            card = self._tool_cards.get(event.call_id)
            if card is None:
                card = ToolCard(event.call_id, event.name)
                self._tool_cards[event.call_id] = card
                await messages.mount(card)
                # Prune old entries from the tracking dict to prevent
                # unbounded growth in long sessions. The widgets stay in the
                # timeline (Lazy-rendered); we only stop tracking them for
                # live updates. Pending approval cards are never pruned.
                if len(self._tool_cards) > self._TOOL_CARD_MAX:
                    for old_id in list(self._tool_cards.keys()):
                        if old_id in self._approval_waiters:
                            continue  # never prune pending approvals
                        if old_id != event.call_id:
                            del self._tool_cards[old_id]
                        if len(self._tool_cards) <= self._TOOL_CARD_MAX:
                            break
            card.start(args=event.args, origin=event.origin, risk=event.risk, started_at=event.started_at)
            status.update(self._status(f"Running {event.name}…"))
            activity.describe_tool(event.name, event.args)
        elif isinstance(event, ToolCallFinished):
            card = self._tool_cards.get(event.call_id)
            if card is not None:
                card.update_result(
                    result=event.result,
                    preview=event.preview,
                    is_error=event.is_error,
                    elapsed_seconds=event.elapsed_seconds,
                    exit_code=event.exit_code,
                )
            activity.describe("Reviewing result", event.name.replace("_", " "))
        elif isinstance(event, ToolApprovalPending):
            card = self._tool_cards.get(event.call_id)
            if card is None:
                card = ToolCard(event.call_id, event.name)
                self._tool_cards[event.call_id] = card
                await messages.mount(card)
            card.mark_approval_pending(event)
            self.query_one(ApprovalPanel).enqueue(event)
            status.update(self._status(f"Approval required: {event.name}"))
            activity.describe("Waiting for approval", event.name.replace("_", " "))
        elif isinstance(event, ToolApprovalResolved):
            approval_panel = self.query_one(ApprovalPanel)
            approval_panel.resolve(event.call_id)
            card = self._tool_cards.get(event.call_id)
            if card is not None:
                card.resolve_approval(approved=event.approved)
            status.update(self._status("Ready" if event.approved else "Denied"))
            if approval_panel.active_request is None:
                self.query_one("#prompt", PromptEditor).focus()
            activity.describe("Continuing", "tool approved" if event.approved else "tool denied")
        elif isinstance(event, ContextCompactionStarted):
            await self._close_assistant_segment()
            await self._update_compaction_row("Compacting context…")
            activity.describe("Compacting context")
        elif isinstance(event, ContextCompactionCompleted):
            await self._update_compaction_row(
                f"Context compacted: {event.active_message_count} active messages."
            )
        elif isinstance(event, ContextCompactionFailed):
            await self._update_compaction_row(f"Context compaction failed: {event.message}")
        elif isinstance(event, UsageUpdated):
            status.update(self._status_line(event))
        elif isinstance(event, RunCompleted):
            # Force a final flush so the rendered markdown reflects every
            # token of the streamed answer before we mark the run done.
            await self._close_assistant_segment()
            status.update(self._status("Ready"))
            activity.stop()
        elif isinstance(event, RunFailed):
            await self._close_assistant_segment()
            await self._append_system(f"Run failed: {event.message}")
            status.update(self._status("Run failed"))
            activity.stop()
        elif isinstance(event, RunCancelled):
            await self._close_assistant_segment()
            await self._append_system("Run cancelled.")
            status.update(self._status("Cancelled"))
            activity.stop()
        await self._finish_timeline_update(messages, follow)

    async def _ensure_assistant_segment(self, messages: VerticalScroll) -> None:
        """Mount one complete Markdown document for an assistant segment."""

        if self._assistant_stream is not None:
            return
        document = AssistantMarkdown("", classes="assistant-message")
        self._assistant_container = document
        await messages.mount(document)

        async def render_markdown(text: str) -> None:
            render_follow = self._capture_timeline_follow(messages)
            document.set_source(text)
            await self._finish_timeline_update(messages, render_follow)

        self._assistant_stream = StreamingMarkdownController(render_markdown)

    async def _close_assistant_segment(self) -> None:
        """Flush and close the active contiguous assistant-text segment.

        The container and its rendered blocks remain in the timeline as the
        answer; we only drop the live-streaming references so the next
        :class:`TextDelta` starts a fresh segment.
        """

        stream = self._assistant_stream
        try:
            if stream is not None:
                await stream.close()
        except Exception as error:
            # Rendering is a view concern. Preserve the runtime's original
            # completed/failed/cancelled event even if Rich rejects a frame.
            self.notify(f"Markdown render failed: {error}", severity="error")
        finally:
            self._assistant_stream = None
            self._assistant_container = None

    async def _flush_assistant_now(self) -> None:
        """Force every buffered token through the serialized renderer."""

        if self._assistant_stream is not None:
            await self._assistant_stream.flush()

    def _status_suffix(self) -> str:
        """Persistent runtime context and keymap hints.

        Kept on every status update so the user always sees which model and
        approval mode are active, plus the most important keymap hints. The
        ``│`` separates the config segment from the keymap segment, and ``·``
        separates items within each segment.
        """

        width = self.size.width if self.is_running else 120
        parts = [self._model_display()]
        if width >= 120:
            parts.extend(
                (
                    str(self.resources.workspace),
                    self.session.id[:8] if self.session is not None else "no-session",
                    "Shift+Tab mode · / commands · @ files · Ctrl+P",
                )
            )
        elif width >= 80:
            parts.append("Shift+Tab · / · @")
        return "  │  " + "  │  ".join(parts)

    def _status(self, state: str) -> str:
        """Build a full status line: ``<state><suffix>``.

        ``state`` is the run-state text ("Thinking…", "Ready", the usage
        summary). We always append the model + keymap suffix so it
        never disappears during a run.
        """

        marker = "▶▶" if self._approval_mode is ApprovalMode.AUTO else "◆"
        usage = self._usage_summary()
        usage_suffix = f" · {usage}" if usage and self.size.width >= 80 else ""
        return f"{marker} {self._approval_mode.value} mode  ·  {state}{usage_suffix}{self._status_suffix()}"

    def _usage_summary(self) -> str:
        event = self._last_usage_event
        if event is None:
            return ""
        usage = event.usage or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        return (
            f"ctx {_fmt_tokens(event.context_tokens_estimate)} · "
            f"req {event.request_count} · tools {event.tool_call_count} · "
            f"{_fmt_tokens(input_tokens)}/{_fmt_tokens(output_tokens)} tok · "
            f"{event.elapsed_seconds:.1f}s"
        )

    def _status_line(self, event: UsageUpdated) -> str:
        self._last_usage_event = event
        return self._status("Ready")

    async def _update_compaction_row(self, text: str) -> None:
        """Render compaction state into one mutable row, not three messages."""

        messages = self.query_one("#messages", VerticalScroll)
        if self._compaction_row is None:
            self._compaction_row = Static("", classes="compaction-row", markup=False)
            await messages.mount(self._compaction_row)
        self._compaction_row.update(text)

    def _clear_compaction_row(self) -> None:
        """Remove the compaction row so a new/resumed session starts clean.

        The row is session-scoped state: it reflects the previous session's
        last compaction and must not linger into a new one. We drop the widget
        reference so the next compaction re-mounts a fresh row.
        """
        if self._compaction_row is not None:
            try:
                self._compaction_row.remove()
            except Exception:
                pass
            self._compaction_row = None

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

        await self._handle_command("/quit")

    def action_follow_tail(self) -> None:
        """Return to the newest activity and re-enable automatic following."""

        self._follow_tail = True
        self._scroll_timeline_end(self.query_one("#messages", VerticalScroll))
        self.query_one("#new-activity", Static).remove_class("visible")

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
        """Switch the approval mode without presenting the auto confirmation.

        This is the state mutation seam used after confirmation and by tests.
        User-facing actions must call :meth:`request_approval_mode`.
        """

        try:
            parsed = ApprovalMode.parse(mode)
        except ValueError as error:
            raise ValueError(
                f"approval mode must be 'manual', 'accept_edits', or 'auto', got {mode!r}"
            ) from error
        if parsed is self._approval_mode:
            return
        self._approval_mode = parsed
        self._refresh_topbar()
        # Refresh the status bar suffix too so the mode badge updates live,
        # not just on the next run event.
        try:
            status = self.query_one("#status", Static)
            status.set_class(parsed is ApprovalMode.AUTO, "mode-auto")
            status.update(self._status("Ready"))
        except Exception:
            pass

    def request_approval_mode(self, mode: str) -> None:
        """Request a mode change, confirming every transition into auto."""

        parsed = ApprovalMode.parse(mode)
        if parsed is not ApprovalMode.AUTO:
            self.set_approval_mode(parsed.value)
            return
        self.push_screen(AutoModeConfirmation(), self._complete_auto_mode_change)

    _request_approval_mode = request_approval_mode

    def _complete_auto_mode_change(self, confirmed: bool | None) -> None:
        if confirmed:
            self.set_approval_mode(ApprovalMode.AUTO.value)
            self.notify("Approval mode: auto", timeout=2)
        else:
            self.notify("AUTO mode cancelled", timeout=2)

    def action_toggle_approval_mode(self) -> None:
        """Cycle manual, accept-edits, and auto modes."""

        modes = (ApprovalMode.MANUAL, ApprovalMode.ACCEPT_EDITS, ApprovalMode.AUTO)
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
        await self.handle_input(self.last_prompt)
