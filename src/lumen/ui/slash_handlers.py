"""Slash-command handlers, extracted verbatim from ``app.py``.

``SlashHandlersMixin`` carries the dispatcher (``_handle_command``) and every
``_cmd_*`` method the registry in ``slash_commands.py`` points at. The
registry's handler strings keep resolving via ``getattr(self, entry.handler)``
because the mixin sits in ``LumenApp``'s MRO — ``slash_commands.py`` itself is
unchanged. ``LumenApp`` is only imported under ``TYPE_CHECKING`` to avoid a
circular import.
"""

# Cooperative Textual mixin; see approval_controller.py for the intersection-
# self limitation behind these local suppressions.
# pyright: reportGeneralTypeIssues=false, reportPrivateUsage=false

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any, cast

from textual.widgets import Static

from lumen.application import CancelClarification, SelectModel
from lumen.approval import ApprovalMode
from lumen.collaboration import CollaborationMode
from lumen.context import (
    ContextCompactCommand,
    ContextEngine,
    ContextMemoryCommand,
    ContextReportCommand,
)
from lumen.run_coordinator import RunInput
from lumen.tools.workspace import Workspace
from lumen.ui.command_gate import (
    CommandPolicy,
    classify_command,
    classify_model_command,
)
from lumen.ui.file_mention import expand_file_mentions
from lumen.ui.slash_commands import find_command, render_help
from lumen.ui.themes import BUILTIN_THEMES

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class SlashHandlersMixin:
    """Dispatch and implement the ``/`` commands registered in slash_commands."""

    def _context_engine(self: LumenApp) -> ContextEngine | None:
        """The active session's context engine, if a runtime is open."""

        runtime = self.resources.runtime
        return runtime.context_engine if runtime is not None else None

    async def _run_context_control(self: LumenApp, command: Any) -> None:
        """Run a read-only/stub context control command and display its result."""

        engine = self._context_engine()
        if engine is None:
            await self._append_system("Context engine is not available yet.")
            return

        async def _noop(_event: Any) -> None:
            return None

        result = await engine.control(command, _noop)
        await self._append_system(self.format_context_result(result))

    async def _render_context(self: LumenApp, parts: list[str]) -> None:
        """Render the ``/context`` budget report (``--json`` for machine output)."""

        if len(parts) > 1 and parts[1] == "sources":
            if self.session is None:
                await self._append_system("No active session.")
                return
            sources = self.resources.context_source_summary(self.session.id)
            await self._append_system(
                "\n".join(
                    f"{source['kind']}  {source['reference']}  {source['revision'][:16]}  {source['status']}"
                    for source in sources
                )
                or "No active context sources."
            )
            return

        engine = self._context_engine()
        if engine is None:
            await self._append_system("Context engine is not available yet.")
            return

        async def _noop(_event: Any) -> None:
            return None

        result = await engine.control(
            ContextReportCommand(session_id=self.session.id if self.session is not None else None),
            _noop,
        )
        if len(parts) > 1 and parts[1] == "--json":
            await self._append_system(json.dumps(result.payload, ensure_ascii=False, indent=2))
            return
        await self._append_system(self.format_context_result(result))

    async def _handle_command(self: LumenApp, line: str) -> None:
        try:
            parts = shlex.split(line)
        except ValueError as error:
            await self._append_system(f"Invalid command: {error}")
            return
        command = parts[0].lower()
        # Gate state-destroying / run-starting commands while an agent run is
        # active. Read-only commands (/help, /mode, /tools, /skills, /sessions)
        # always pass; /new, /resume, /model <name> are refused; /retry and
        # /skill:* are refused (they start a new run); /exit cancels first.
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
        # /skill:<name> [args] — manually trigger a skill. Kept as a special
        # prefix branch outside the registry because the skill name is dynamic.
        if command.startswith("/skill:"):
            await self._cmd_skill_invoke(parts, line)
            return
        # Registry dispatch: look up the command (aliases included) and call
        # its handler. Unknown commands keep the existing error behaviour.
        entry = find_command(command)
        if entry is None or not entry.handler:
            await self._append_system(f"Unknown command: {command}. Use /help.")
            return
        await getattr(self, entry.handler)(parts, line)

    async def _unknown_command(self: LumenApp, command: str) -> None:
        """Emit the standard unknown-command message.

        Also used by handlers whose subcommand form doesn't match (e.g. a
        bare ``/skill``), preserving the old fall-through behaviour.
        """

        await self._append_system(f"Unknown command: {command}. Use /help.")

    async def _cmd_prompt(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/prompt <server:name> [key=value ...]`` — render an MCP prompt template and submit it."""

        if len(parts) < 2:
            await self._append_system("Usage: /prompt <server:name> [key=value ...]")
            return
        arguments: dict[str, str] = {}
        for item in parts[2:]:
            if "=" not in item:
                await self._append_system(f"Invalid prompt argument {item!r}; use key=value.")
                return
            key, value = item.split("=", 1)
            arguments[key] = value
        try:
            rendered = await self.resources.render_mcp_prompt(parts[1], arguments)
        except Exception as error:
            await self._append_system(f"Cannot render MCP prompt: {error}")
            return
        await self._append_user(raw)
        self.current_worker = self.run_worker(
            self._run_prompt(RunInput(display_text=raw, model_prompt=rendered)),
            name="agent-run",
            exclusive=True,
        )

    async def _cmd_skill_invoke(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/skill:<name> [args]`` — manually trigger a skill.

        The skill body is expanded into a <skill> XML block and sent as a user
        message, so the model receives the full instructions in-context. This
        works even for skills with disable-model-invocation: true.
        """

        command = parts[0].lower()
        skill_name = command[len("/skill:") :]
        skill = self.resources.load_skill_by_name(skill_name)
        if skill is None:
            await self._append_system(f"Unknown skill: {skill_name}. Use /skills to list available skills.")
            return
        if self.session is not None:
            self.resources.activate_skill(self.session.id, skill_name)
        args = shlex.join(parts[1:]) if len(parts) > 1 else ""
        # Persist the exact user input. Skill instructions are model-only,
        # so timeline browsing and /retry never expose expanded XML.
        display = raw
        await self._append_user(display)
        invocation = f"Apply the active Skill `{skill.name}` to this request."
        if args:
            invocation = f"{invocation}\n\nUser arguments:\n{args}"
        expanded_prompt = expand_file_mentions(invocation, Workspace(self.resources.workspace))
        self.current_worker = self.run_worker(
            self._run_prompt(RunInput(display_text=display, model_prompt=expanded_prompt)),
            name="agent-run",
            exclusive=True,
        )

    async def _cmd_skills(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/skills`` — list the discovered Agent Skills."""

        if not self.resources.skills:
            await self._append_system(
                "No skills found. Add SKILL.md files to .lumen/skills/ or ~/.lumen/skills/."
            )
            return
        rows = [f"  {s.name}  —  {s.description[:80]}" for s in self.resources.skills]
        await self._append_system(f"{len(self.resources.skills)} skill(s) available:\n" + "\n".join(rows))

    async def _cmd_skill(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/skill unload <name>`` — unload a skill from this session."""

        if not (len(parts) == 3 and parts[1] == "unload"):
            await self._unknown_command(parts[0].lower())
            return
        if self.session is None:
            await self._append_system("No active session.")
            return
        self.resources.deactivate_context_source(self.session.id, "skill", parts[2])
        await self._append_system(f"Unloaded Skill {parts[2]!r} from this session.")

    async def _cmd_clarification(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/clarification cancel`` — cancel the pending clarification."""

        if not (len(parts) == 2 and parts[1] == "cancel"):
            await self._unknown_command(parts[0].lower())
            return
        if self.session is None:
            await self._append_system("No active session.")
            return
        await self.workspace_host.dispatch(CancelClarification(self.session.id))
        await self._append_system("Cancelled the pending clarification.")

    async def _cmd_help(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/help`` — render the grouped command reference from the registry."""

        await self._append_system(render_help())

    async def _cmd_clear(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/clear`` — clear the visible timeline, keeping session context."""

        await self._clear_visible_timeline()

    async def _cmd_copy(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/copy`` — copy the latest assistant response."""

        self.action_copy_last_response()

    async def _cmd_new(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/new`` — start a fresh session."""

        # A new session must NOT inherit the previous session's compaction
        # summary or its compaction row — those belong to the old session.
        # Clearing here is the isolation boundary: no App-level state
        # crosses the /new seam.
        self._clear_compaction_row()
        await self._apply_coordinator_state(await self.coordinator.new_session())
        assert self.session is not None
        await self._append_system(f"Started new session {self.session.id}")
        self._refresh_topbar()

    async def _cmd_model(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/model [name]`` — list configured models, or switch the active model."""

        if len(parts) == 1:
            # List configured models with the active one marked.
            available = self.resources.available_models()
            active = self.resources.active_model_name()
            rows: list[str] = []
            for name in available:
                model_cfg = self.resources.model_registry[name]
                marker = "* " if name == active else "  "
                rows.append(f"{marker}{name}  ->  {model_cfg.id}")
            await self._append_system("\n".join(rows) or "No models configured.")
            return
        if len(parts) != 2:
            await self._unknown_command(parts[0].lower())
            return
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
            await self.workspace_host.dispatch(SelectModel(target))
        except Exception as error:
            await self._append_system(f"Failed to switch model: {error}")
            return
        self._refresh_topbar()
        self.query_one("#status", Static).update(self._status("Ready"))
        await self._append_system(f"Switched to {target} ({self.resources.active_model_config().id}).")

    async def _cmd_mode(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/mode [manual|accept_edits|plan|auto]`` — show or switch the approval mode."""

        if len(parts) == 1:
            # Report the current mode and what it means, so the user knows what
            # they're toggling without having to read the README.
            is_plan = self._collaboration_mode is CollaborationMode.PLAN
            current = self._approval_mode
            behaviour = (
                "auto-approving classified tools (unknown remote tools still require confirmation)"
                if current is ApprovalMode.AUTO
                else (
                    "read-only exploration; file changes and commands are blocked"
                    if is_plan
                    else (
                        "auto-approving builtin file edits; confirming commands and remote writes"
                        if current is ApprovalMode.ACCEPT_EDITS
                        else "allowing reads; confirming edits, commands, and external actions"
                    )
                )
            )
            await self._append_system(
                f"Mode: {'plan' if is_plan else current.value} ({behaviour}). "
                "Press Shift+Tab to cycle, or use /mode manual|accept_edits|plan|auto."
            )
            return
        if len(parts) != 2:
            await self._unknown_command(parts[0].lower())
            return
        target_mode = parts[1].lower()
        if target_mode not in {"manual", "accept_edits", "plan", "auto"}:
            await self._append_system(
                f"Unknown mode {parts[1]!r}. Use 'manual', 'accept_edits', 'plan', or 'auto'."
            )
            return
        self._request_approval_mode(target_mode)

    async def _cmd_status(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/status`` — detailed context moved out of the idle welcome panel."""

        if len(parts) != 1:
            await self._append_system("Usage: /status")
            return
        session_id = self.session.id if self.session is not None else "starting"
        mode = (
            "plan"
            if self._collaboration_mode is CollaborationMode.PLAN
            else self._approval_mode.value
        )
        lines = [
            f"Agent: {self.config.agent.name}",
            f"Model: {self._model_display()}",
            f"Session: {session_id}",
            f"Workspace: {self.resources.workspace}",
            f"Mode: {mode}",
            f"Sandbox: {self.config.sandbox.mode} · network {'on' if self.config.sandbox.network else 'off'}",
            f"Transcript: {self._transcript_density} · "
            f"animations {'on' if self.config.ui.animations else 'off'}",
            f"Resources: {len(self.resources.tool_metadata)} tools · "
            f"{len(self.resources.skills)} skills · MCP {self._mcp_summary()}",
        ]
        await self._append_system("\n".join(lines))

    async def _cmd_transcript(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/transcript`` — open searchable structured transcript."""

        if len(parts) != 1:
            await self._append_system("Usage: /transcript")
            return
        self.action_show_transcript()

    async def _cmd_theme(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/theme [name]`` — list builtin themes, or switch the active one."""

        if len(parts) == 1:
            # List the registered themes with the active one marked, mirroring
            # the bare ``/model`` listing form.
            rows = [
                f"{'* ' if name == self.theme else '  '}{name}  ({'dark' if theme.dark else 'light'})"
                for name, theme in BUILTIN_THEMES.items()
            ]
            await self._append_system("\n".join(rows))
            return
        if len(parts) != 2:
            await self._unknown_command(parts[0].lower())
            return
        target = parts[1]
        if target not in BUILTIN_THEMES:
            await self._append_system(f"Unknown theme {target!r}. Available: {sorted(BUILTIN_THEMES)}")
            return
        if target == self.theme:
            await self._append_system(f"Already on {target}.")
            return
        # Textual re-renders against the new palette immediately; this only
        # affects the live session — persist via ui.theme in agent.yaml.
        self.theme = target
        await self._append_system(f"Switched to {target}.")

    async def _list_sessions(self: LumenApp) -> None:
        """Render the stored session list (shared by ``/sessions`` and bare ``/resume``)."""

        sessions = self.resources.session_repository.list()
        await self._append_system(
            "\n".join(f"{item.id}  {item.created_at}  {item.model_id}" for item in sessions)
            or "No sessions found."
        )

    async def _cmd_sessions(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/sessions`` — list past sessions."""

        if len(parts) != 1:
            await self._unknown_command(parts[0].lower())
            return
        await self._list_sessions()

    async def _cmd_children(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/agents`` (or legacy ``/children``) — inspect or interrupt Agents."""

        if len(parts) == 1:
            self.action_show_children()
            return
        if len(parts) == 3 and parts[1].lower() in {"cancel", "interrupt"}:
            try:
                child = await self.coordinator.cancel_child_run(parts[2])
            except Exception as error:
                await self._append_system(f"Cannot interrupt Agent: {error}")
            else:
                await self._append_system(
                    f"Agent {child.get('id', parts[2])}: {child.get('status', 'interrupted')}"
                )
            return
        if len(parts) >= 4 and parts[1].lower() in {"message", "continue"}:
            content = raw.split(maxsplit=3)[3]
            try:
                if parts[1].lower() == "message":
                    await self.coordinator.send_agent_message(parts[2], content)
                    outcome = "message queued"
                else:
                    await self.coordinator.continue_agent(parts[2], content)
                    outcome = "follow-up started"
            except Exception as error:
                await self._append_system(f"Agent action failed: {error}")
            else:
                await self._append_system(f"Agent {parts[2]}: {outcome}")
            return
        if len(parts) == 3 and parts[1].lower() in {"import", "reject"}:
            try:
                if parts[1].lower() == "import":
                    await self.coordinator.approve_agent_import(parts[2])
                    outcome = "import handled"
                else:
                    await self.coordinator.reject_agent_import(parts[2])
                    outcome = "changes rejected"
            except Exception as error:
                await self._append_system(f"Agent action failed: {error}")
            else:
                await self._append_system(f"Agent {parts[2]}: {outcome}")
            return
        if len(parts) >= 4 and parts[1].lower() == "close":
            reason = raw.split(maxsplit=3)[3]
            try:
                await self.coordinator.close_agent(parts[2], reason)
            except Exception as error:
                await self._append_system(f"Agent action failed: {error}")
            else:
                await self._append_system(f"Agent {parts[2]}: closed")
            return
        await self._append_system(
            "Usage: /agents [interrupt|message|continue|import|reject|close] <id> [text]"
        )

    async def _cmd_checkpoints(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/checkpoints`` — safely branch session context at an earlier turn."""

        if len(parts) != 1:
            await self._append_system("Usage: /checkpoints")
            return
        self.action_show_checkpoints()

    async def _cmd_resume(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/resume <id>`` — resume a session by id (bare form lists sessions)."""

        if len(parts) == 1:
            await self._list_sessions()
            return
        if len(parts) != 2:
            await self._unknown_command(parts[0].lower())
            return
        try:
            state = await self.coordinator.resume(parts[1])
        except Exception as error:
            await self._append_system(f"Cannot resume session: {error}")
        else:
            await self._apply_coordinator_state(state, restored=True)
            await self._append_system(f"Resumed session {state.session.id}")
            self._refresh_topbar()

    async def _cmd_tools(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/tools`` — list the model-visible tools."""

        lines = [
            f"{name}  [{metadata['risk']}]  {metadata['origin']}"
            for name, metadata in sorted(self.resources.tool_metadata.items())
        ]
        await self._append_system("\n".join(lines) or "No tools enabled.")

    async def _cmd_mcp(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/mcp`` — show MCP connections and deferred schema status."""

        mcp_rows = self.resources.mcp_summary()
        if not mcp_rows:
            await self._append_system("No MCP servers configured.")
        else:
            await self._append_system(
                "\n".join(
                    f"{row['name']}  [{row['status']}]  {row['scope']} · {row['approval']} · "
                    f"{row['tools']} tools · "
                    f"{row['deferred']} deferred · {row['always_loaded']} always loaded"
                    for row in mcp_rows
                )
                + "\nUse /context --json to inspect the current discovered MCP working set."
            )

    async def _cmd_hooks(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/hooks`` — list configured hooks and statistics."""

        hooks = self.resources.hook_summary()
        if not hooks:
            await self._append_system("No hooks configured.")
        else:
            await self._append_system(
                "\n".join(
                    f"{row['event']}  {row['matcher']}  {row['runner']}  "
                    f"denied={row['deny_count']}  last={row['last_triggered'] or '-'}"
                    for row in hooks
                )
            )

    async def _cmd_resources(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/resources`` — list MCP resources available for explicit context loading."""

        resources = self.resources.mcp_resource_summary(self.session.id if self.session is not None else None)
        await self._append_system(
            "\n".join(
                f"{'*' if row['active'] else ' '} {row['reference']}  {row['description']}"
                for row in resources
            )
            or "No MCP resources available."
        )

    async def _cmd_resource(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/resource [refresh|unload] <ref>`` — load, refresh, or unload one MCP resource."""

        if len(parts) == 2:
            if self.session is None:
                await self._append_system("No active session.")
                return
            try:
                document = await self.resources.activate_mcp_resource(self.session.id, parts[1])
            except Exception as error:
                await self._append_system(f"Cannot load MCP resource: {error}")
            else:
                await self._append_system(
                    f"Loaded {document['server']}::{document['uri']} into retrieved context."
                )
            return
        if len(parts) == 3 and parts[1] == "refresh":
            if self.session is None:
                await self._append_system("No active session.")
                return
            try:
                document = await self.resources.activate_mcp_resource(self.session.id, parts[2])
            except Exception as error:
                await self._append_system(f"Cannot refresh MCP resource: {error}")
            else:
                await self._append_system(
                    f"Refreshed {document['server']}::{document['uri']} for this session."
                )
            return
        if len(parts) == 3 and parts[1] == "unload":
            if self.session is None:
                await self._append_system("No active session.")
                return
            self.resources.deactivate_context_source(self.session.id, "resource", parts[2])
            await self._append_system(f"Unloaded MCP resource {parts[2]!r} from this session.")
            return
        await self._unknown_command(parts[0].lower())

    async def _cmd_prompts(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/prompts`` — list MCP prompt templates."""

        prompts = self.resources.mcp_prompt_summary()
        await self._append_system(
            "\n".join(
                f"{row['reference']}  "
                f"args={','.join(cast(tuple[str, ...], row['arguments'])) or '-'}  "
                f"{row['description']}"
                for row in prompts
            )
            or "No MCP prompts available."
        )

    async def _cmd_context(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/context [--json|sources]`` — render the context budget report."""

        await self._render_context(parts)

    async def _cmd_compact(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/compact [focus]`` — force a compaction."""

        focus = shlex.join(parts[1:]) if len(parts) > 1 else None
        session_id = self.session.id if self.session is not None else None
        await self._run_context_control(ContextCompactCommand(focus=focus, session_id=session_id))

    async def _cmd_memory(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/memory <action> [...]`` — memory control (list, remember, edit, forget, use, learn, …)."""

        action = parts[1] if len(parts) > 1 else "list"
        payload: dict[str, Any] = {}
        if action == "remember":
            args = parts[2:]
            if "--scope" in args:
                scope_index = args.index("--scope")
                if scope_index + 1 < len(args):
                    payload["scope"] = args[scope_index + 1]
                args = args[:scope_index]
            payload["content"] = " ".join(args)
            if self.session is not None:
                payload["session_id"] = self.session.id
        elif action == "forget":
            payload["target"] = " ".join(parts[2:])
        elif action == "edit":
            if len(parts) < 3:
                await self._append_system("Use /memory edit <id> [--apply | --set <new content>]")
                return
            payload["target"] = parts[2]
            if "--apply" in parts[3:]:
                payload["apply"] = True
            elif "--set" in parts[3:]:
                set_index = parts.index("--set")
                payload["content"] = " ".join(parts[set_index + 1 :])
        elif action in {"use", "learn", "incognito"}:
            value = parts[2].lower() if len(parts) > 2 else "on"
            if value not in {"on", "off"}:
                await self._append_system(f"Use /memory {action} on|off")
                return
            payload["enabled"] = value == "on"
        await self._run_context_control(ContextMemoryCommand(action=action, payload=payload))

    async def _cmd_retry(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/retry`` — re-send the last prompt."""

        if self.last_prompt is None:
            await self._append_system("There is no previous prompt to retry.")
        else:
            await self.handle_input(self.last_prompt, is_retry=True)

    async def _cmd_exit(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/exit`` (alias ``/quit``) — exit the app."""

        self.exit()
