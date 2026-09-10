"""Slash command registry — the single source of truth for TUI slash commands.

Every consumer derives from this module instead of keeping its own hand-written
list:

* dispatch (``app._handle_command``) looks up the entry and calls its handler;
* ``/help`` renders the grouped, per-command reference via :func:`render_help`;
* the completion dropdown lists :func:`iter_visible` entries plus their
  ``completion_rows``;
* the run-time safety gate (``command_gate``) maps ``while_running`` to a
  ``CommandPolicy``;
* the command palette (``commands.py``) pulls help text from ``in_palette``
  entries.

``/skill:<name>`` keeps its special prefix-parsing branch in the dispatcher
(the name is dynamic); the registry only carries its help/completion text via
the ``skill:`` pseudo-entry.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from lumen.branding import FRAMEWORK_NAME

# Run-safety policies, mirroring command_gate.CommandPolicy values (kept as
# plain strings so this module stays import-free of the gate).
ALLOW = "allow"
BLOCK = "block"
QUEUE = "queue"
CANCEL_THEN_RUN = "cancel_then_run"

# Help categories in display order.
CATEGORY_SESSION = "Session"
CATEGORY_MODEL = "Model"
CATEGORY_CONTEXT = "Context"
CATEGORY_MCP = "MCP"
CATEGORY_MEMORY = "Memory"
CATEGORY_OTHER = "Other"
CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_SESSION,
    CATEGORY_MODEL,
    CATEGORY_CONTEXT,
    CATEGORY_MCP,
    CATEGORY_MEMORY,
    CATEGORY_OTHER,
)


@dataclass(frozen=True)
class SlashCommand:
    """One slash command.

    ``name`` is the command without the leading slash (``"model"``); the
    ``"skill:"`` pseudo-entry documents the dynamic ``/skill:<name>`` form.
    ``handler`` is the ``LumenApp`` method that runs the command
    (``_cmd_xxx(parts, raw)``); empty for the dispatcher-handled ``skill:``
    branch. ``while_running`` is the run-safety policy consumed by
    ``command_gate``. ``completion_rows`` are extra subcommand rows shown in
    the completion dropdown as ``/<name> <suffix>``.
    """

    name: str
    usage: str
    description: str
    category: str
    handler: str
    while_running: str = ALLOW
    aliases: tuple[str, ...] = ()
    in_palette: bool = False
    hidden: bool = False
    completion_rows: tuple[tuple[str, str], ...] = ()


_COMMANDS: tuple[SlashCommand, ...] = (
    # --- session lifecycle -------------------------------------------------
    SlashCommand(
        name="new",
        usage="/new",
        description="Start a fresh session",
        category=CATEGORY_SESSION,
        handler="_cmd_new",
        while_running=BLOCK,
        in_palette=True,
    ),
    SlashCommand(
        name="sessions",
        usage="/sessions",
        description="List past sessions",
        category=CATEGORY_SESSION,
        handler="_cmd_sessions",
        in_palette=True,
    ),
    SlashCommand(
        name="agents",
        usage="/agents [interrupt <id>]",
        description="Inspect or interrupt persistent Agents",
        category=CATEGORY_SESSION,
        handler="_cmd_children",
        in_palette=False,
        hidden=True,
        completion_rows=(("interrupt", "Interrupt one Agent"),),
    ),
    SlashCommand(
        name="children",
        usage="/children [cancel <id>]",
        description="Inspect or cancel persistent child tasks",
        category=CATEGORY_SESSION,
        handler="_cmd_children",
        in_palette=True,
        completion_rows=(("cancel", "Cancel one child task"),),
    ),
    SlashCommand(
        name="checkpoints",
        usage="/checkpoints",
        description="Browse receipts and rewind into a new session branch",
        category=CATEGORY_SESSION,
        handler="_cmd_checkpoints",
        while_running=BLOCK,
        in_palette=True,
    ),
    SlashCommand(
        name="resume",
        usage="/resume <id>",
        description="Resume a session by id",
        category=CATEGORY_SESSION,
        handler="_cmd_resume",
        while_running=BLOCK,
        in_palette=True,
    ),
    SlashCommand(
        name="clear",
        usage="/clear",
        description="Clear the visible timeline · keep session context",
        category=CATEGORY_SESSION,
        handler="_cmd_clear",
        while_running=BLOCK,
        in_palette=True,
    ),
    SlashCommand(
        name="retry",
        usage="/retry",
        description="Re-send the last prompt",
        category=CATEGORY_SESSION,
        handler="_cmd_retry",
        while_running=QUEUE,
        in_palette=True,
    ),
    SlashCommand(
        name="edit",
        usage="/edit",
        description="Edit the last prompt in $EDITOR and resend on a fresh branch",
        category=CATEGORY_SESSION,
        handler="_cmd_edit",
        while_running=BLOCK,
        in_palette=True,
    ),
    # --- model / approval ---------------------------------------------------
    SlashCommand(
        name="thinking",
        usage="/thinking [level]",
        description="Choose reasoning effort for this Session",
        category=CATEGORY_MODEL,
        handler="_cmd_thinking",
        in_palette=True,
    ),
    SlashCommand(
        name="model",
        usage="/model [name]",
        description="Choose a model or switch by name",
        category=CATEGORY_MODEL,
        handler="_cmd_model",
        # The picker is read-only during a run; switching rebuilds the
        # runtime. The gate refines this via classify_model_command.
        in_palette=True,
    ),
    SlashCommand(
        name="mode",
        usage="/mode [manual|accept_edits|plan|auto]",
        description="Choose a permission mode or switch by name",
        category=CATEGORY_MODEL,
        handler="_cmd_mode",
        in_palette=True,
    ),
    SlashCommand(
        name="tasks",
        usage="/tasks",
        description="Show or collapse the latest task plan",
        category=CATEGORY_SESSION,
        handler="_cmd_tasks",
    ),
    SlashCommand(
        name="status",
        usage="/status",
        description="Show workspace, session, modes and UI settings",
        category=CATEGORY_MODEL,
        handler="_cmd_status",
        in_palette=True,
    ),
    # --- context -------------------------------------------------------------
    SlashCommand(
        name="context",
        usage="/context [--json|sources]",
        description="Show context budget · zones, blocks, pressure",
        category=CATEGORY_CONTEXT,
        handler="_cmd_context",
    ),
    SlashCommand(
        name="instructions",
        usage="/instructions [--json]",
        description="查看稳定 prompt、动态上下文和来源摘要",
        category=CATEGORY_CONTEXT,
        handler="_cmd_instructions",
    ),
    SlashCommand(
        name="compact",
        usage="/compact [focus]",
        description="Force a compaction (M4)",
        category=CATEGORY_CONTEXT,
        handler="_cmd_compact",
    ),
    SlashCommand(
        name="clarification",
        usage="/clarification cancel",
        description="Cancel the pending clarification",
        category=CATEGORY_CONTEXT,
        handler="_cmd_clarification",
        completion_rows=(("cancel", "Cancel the pending clarification"),),
    ),
    # --- MCP ------------------------------------------------------------------
    SlashCommand(
        name="mcp",
        usage="/mcp",
        description="MCP connections and deferred schema status",
        category=CATEGORY_MCP,
        handler="_cmd_mcp",
    ),
    SlashCommand(
        name="resources",
        usage="/resources",
        description="List MCP resources available for explicit context loading",
        category=CATEGORY_MCP,
        handler="_cmd_resources",
    ),
    SlashCommand(
        name="resource",
        usage="/resource [refresh|unload] <ref>",
        description="Load one MCP resource into the retrieved-context zone",
        category=CATEGORY_MCP,
        handler="_cmd_resource",
        completion_rows=(
            ("refresh", "Refresh one session resource snapshot"),
            ("unload", "Unload one resource from this session"),
        ),
    ),
    SlashCommand(
        name="prompts",
        usage="/prompts",
        description="List MCP prompt templates",
        category=CATEGORY_MCP,
        handler="_cmd_prompts",
    ),
    SlashCommand(
        name="prompt",
        usage="/prompt <server:name> [key=value ...]",
        description="Render and submit an MCP prompt template",
        category=CATEGORY_MCP,
        handler="_cmd_prompt",
        while_running=QUEUE,
    ),
    # --- memory ---------------------------------------------------------------
    SlashCommand(
        name="memory",
        usage="/memory [list|remember|edit|forget|use|learn|incognito|rebuild]",
        description="Memory: list, remember, edit, forget, use, learn, rebuild",
        category=CATEGORY_MEMORY,
        handler="_cmd_memory",
    ),
    # --- other ------------------------------------------------------------------
    SlashCommand(
        name="theme",
        usage="/theme [lumen-dark|lumen-light]",
        description="List available themes or switch the active UI theme",
        category=CATEGORY_OTHER,
        handler="_cmd_theme",
    ),
    SlashCommand(
        name="transcript",
        usage="/transcript",
        description="Open searchable structured transcript",
        category=CATEGORY_OTHER,
        handler="_cmd_transcript",
        in_palette=True,
    ),
    SlashCommand(
        name="help",
        usage="/help",
        description="Show this command reference",
        category=CATEGORY_OTHER,
        handler="_cmd_help",
    ),
    SlashCommand(
        name="tools",
        usage="/tools",
        description="List model-visible tools",
        category=CATEGORY_OTHER,
        handler="_cmd_tools",
        in_palette=True,
    ),
    SlashCommand(
        name="hooks",
        usage="/hooks",
        description="List configured hooks and statistics",
        category=CATEGORY_OTHER,
        handler="_cmd_hooks",
        in_palette=True,
    ),
    SlashCommand(
        name="copy",
        usage="/copy",
        description="Copy the latest assistant response",
        category=CATEGORY_OTHER,
        handler="_cmd_copy",
        in_palette=True,
    ),
    SlashCommand(
        name="skills",
        usage="/skills",
        description="List available skills",
        category=CATEGORY_OTHER,
        handler="_cmd_skills",
    ),
    SlashCommand(
        name="skill:",
        usage="/skill:<name> [args]",
        description="Manually trigger a skill",
        category=CATEGORY_OTHER,
        handler="",  # dynamic prefix form; dispatched by app._handle_command
        while_running=QUEUE,
    ),
    SlashCommand(
        name="skill",
        usage="/skill unload <name>",
        description="Unload a skill from this session",
        category=CATEGORY_OTHER,
        handler="_cmd_skill",
        completion_rows=(("unload", "Unload one skill from this session"),),
    ),
    SlashCommand(
        name="exit",
        usage="/exit",
        description=f"Exit {FRAMEWORK_NAME}",
        category=CATEGORY_OTHER,
        handler="_cmd_exit",
        while_running=CANCEL_THEN_RUN,
        # /quit stays as a hidden alias for existing users: it dispatches and
        # gates like /exit but is kept out of /help and completions.
        aliases=("quit",),
        in_palette=True,
    ),
)

_BY_NAME: dict[str, SlashCommand] = {}
for _command in _COMMANDS:
    _BY_NAME[_command.name] = _command
    for _alias in _command.aliases:
        _BY_NAME[_alias] = _command


def find_command(name: str) -> SlashCommand | None:
    """Look up a command by name (case-insensitive, leading ``/`` tolerated).

    Aliases resolve to their canonical entry, so ``find_command("quit")``
    returns the ``exit`` entry.
    """

    return _BY_NAME.get(name.lower().lstrip("/"))


def iter_visible() -> Iterator[SlashCommand]:
    """Yield non-hidden commands in registry (help) order."""

    return (command for command in _COMMANDS if not command.hidden)


def render_help() -> str:
    """Render the grouped ``/help`` reference.

    One header per category; each command is ``usage  —  description``,
    aligned within its group. Hidden commands are omitted.
    """

    lines: list[str] = []
    for category in CATEGORY_ORDER:
        entries = [command for command in _COMMANDS if command.category == category and not command.hidden]
        if not entries:
            continue
        if lines:
            lines.append("")
        lines.append(f"{category}:")
        width = max(len(command.usage) for command in entries)
        for command in entries:
            lines.append(f"  {command.usage.ljust(width)}  —  {command.description}")
    return "\n".join(lines)


__all__ = [
    "ALLOW",
    "BLOCK",
    "CANCEL_THEN_RUN",
    "CATEGORY_ORDER",
    "QUEUE",
    "SlashCommand",
    "find_command",
    "iter_visible",
    "render_help",
]
