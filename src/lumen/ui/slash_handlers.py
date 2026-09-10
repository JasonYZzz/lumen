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
from uuid import uuid4

from textual.widgets import Static

from lumen.application import (
    CancelClarification,
    ContextControl,
    GetBootstrap,
    GetInstructions,
    InvokePrompt,
    InvokeSkill,
    ListContextSources,
    ListHooks,
    ListMcpPrompts,
    ListMcpResources,
    SelectModel,
    SelectReasoning,
    SetContextSource,
)
from lumen.collaboration import CollaborationMode
from lumen.context import ContextControlResult
from lumen.reasoning import ReasoningLevel
from lumen.ui.choice_picker import ChoicePickerScreen
from lumen.ui.command_gate import (
    CommandPolicy,
    classify_command,
    classify_model_command,
)
from lumen.ui.composer import edit_text_external
from lumen.ui.slash_commands import find_command, render_help
from lumen.ui.themes import BUILTIN_THEMES

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class SlashHandlersMixin:
    """Dispatch and implement the ``/`` commands registered in slash_commands."""

    async def _run_context_control(
        self: LumenApp,
        control: str,
        *,
        focus: str | None = None,
        action: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Run context control through the shared WorkspaceHost Seam."""

        if self.session is None:
            await self._append_system("No active session.")
            return
        result = await self.workspace_host.dispatch(
            ContextControl(
                self.session.id,
                cast(Any, control),
                focus=focus,
                action=action,
                payload=payload or {},
            )
        )
        rendered = ContextControlResult(
            status=cast(Any, result).status,
            message=str(cast(Any, result).data.get("message", "")),
            payload=dict(cast(Any, result).data.get("payload", {})),
        )
        await self._append_system(self.format_context_result(rendered))

    async def _render_context(self: LumenApp, parts: list[str]) -> None:
        """Render the ``/context`` budget report (``--json`` for machine output)."""

        if len(parts) > 1 and parts[1] == "capabilities":
            await self._append_system(
                json.dumps(self.workspace_host.capabilities(), ensure_ascii=False, indent=2)
            )
            return

        if len(parts) > 1 and parts[1] == "sources":
            if self.session is None:
                await self._append_system("No active session.")
                return
            listed = await self.workspace_host.dispatch(ListContextSources(self.session.id))
            sources = cast(list[dict[str, str]], cast(Any, listed).data.get("items", []))
            await self._append_system(
                "\n".join(
                    f"{source['kind']}  {source['reference']}  {source['revision'][:16]}  {source['status']}"
                    for source in sources
                )
                or "No active context sources."
            )
            return

        if self.session is None:
            await self._append_system("No active session.")
            return
        response = await self.workspace_host.dispatch(ContextControl(self.session.id, "report"))
        result = ContextControlResult(
            status=cast(Any, response).status,
            message=str(cast(Any, response).data.get("message", "")),
            payload=dict(cast(Any, response).data.get("payload", {})),
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
            if command in {"/model", "/thinking"}:
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
        await self._append_user(raw)
        if self.session is None:
            await self._append_system("No active session.")
            return
        try:
            await self.coordinator.set_modes(
                self._approval_mode.value,
                self._collaboration_mode.value,
            )
            started = await self.workspace_host.dispatch(
                InvokePrompt(
                    self.session.id,
                    parts[1],
                    arguments,
                    raw,
                    f"tui-prompt-{uuid4()}",
                )
            )
        except Exception as error:
            await self._append_system(f"Cannot render MCP prompt: {error}")
            return
        self.current_worker = self.run_worker(
            self._consume_started_run(started.run_id),
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
        if self.session is None:
            await self._append_system("No active session.")
            return
        args = shlex.join(parts[1:]) if len(parts) > 1 else ""
        # Persist the exact user input. Skill instructions are model-only,
        # so timeline browsing and /retry never expose expanded XML.
        display = raw
        await self._append_user(display)
        try:
            await self.coordinator.set_modes(
                self._approval_mode.value,
                self._collaboration_mode.value,
            )
            started = await self.workspace_host.dispatch(
                InvokeSkill(
                    self.session.id,
                    skill_name,
                    args,
                    f"tui-skill-{uuid4()}",
                )
            )
        except Exception as error:
            await self._append_system(f"Cannot invoke Skill: {error}")
            return
        self.current_worker = self.run_worker(
            self._consume_started_run(started.run_id),
            name="agent-run",
            exclusive=True,
        )

    async def _cmd_skills(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/skills`` — list the discovered Agent Skills."""

        bootstrap = await self.workspace_host.dispatch(GetBootstrap())
        skills = cast(Any, bootstrap).skills
        if not skills:
            await self._append_system(
                "No skills found. Add SKILL.md files to .lumen/skills/ or ~/.lumen/skills/."
            )
            return
        rows = [f"  {skill['name']}  —  {skill['description'][:80]}" for skill in skills]
        await self._append_system(f"{len(skills)} skill(s) available:\n" + "\n".join(rows))

    async def _cmd_skill(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/skill unload <name>`` — unload a skill from this session."""

        if not (len(parts) == 3 and parts[1] == "unload"):
            await self._unknown_command(parts[0].lower())
            return
        if self.session is None:
            await self._append_system("No active session.")
            return
        await self.workspace_host.dispatch(
            SetContextSource(self.session.id, "skill", parts[2], False)
        )
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
        """``/model [name]`` — open the picker, or switch through the Host."""

        if len(parts) == 1:
            self.action_choose_model()
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

    async def _cmd_thinking(self: LumenApp, parts: list[str], raw: str) -> None:
        if self.session is None:
            return
        try:
            if len(parts) == 1:
                snapshot = await self.workspace_host.snapshot(self.session.id)
                selection = snapshot.reasoning
                if len(selection.supported_levels) < 2:
                    await self._append_system(
                        "This model does not support reasoning control."
                        if selection.capability_status == "unsupported" else
                        "Reasoning control is not configured for this model and API route. "
                        "Provider defaults are preserved; declare verified reasoning_levels "
                        "to enable selection."
                    )
                    return

                def chosen(value: str | None) -> None:
                    if value is not None:
                        self.run_worker(self._cmd_thinking(["/thinking", value], ""))

                self.push_screen(ChoicePickerScreen(
                    [(level.value, level.value.replace("_", " ") + (
                        f" ({selection.provider_default_level.value})"
                        if level is ReasoningLevel.PROVIDER_DEFAULT and selection.provider_default_level else
                        f" → {selection.level_map[level].value}"
                        if level in selection.level_map and selection.level_map[level] != level else ""
                    )) for level in selection.supported_levels if level not in selection.level_map
                     or selection.level_map[level] == level or level == selection.requested
                     or selection.level_map[level] not in selection.supported_levels],
                    selection.requested or ReasoningLevel.PROVIDER_DEFAULT,
                    title="Thinking effort",
                    hint="Provider default sends no effort; a documented default is shown in parentheses.",
                    read_only=bool(snapshot.active_run_id),
                ), chosen)
                return
            if len(parts) != 2:
                raise ValueError("Usage: /thinking [provider_default|off|minimal|low|medium|high|xhigh|max]")
            result = await self.workspace_host.dispatch(
                SelectReasoning(self.session.id, ReasoningLevel(parts[1])),
            )
            effective = result.data["reasoning"]["effective"]
            label = f"{parts[1]} → {effective}" if effective and effective != parts[1] else parts[1]
            await self._append_system(f"Thinking: {label}. Applies to the next run in this Session.")
        except (ValueError, RuntimeError) as error:
            await self._append_system(str(error))

    async def _cmd_mode(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/mode [manual|accept_edits|plan|auto]`` — show or switch the approval mode."""

        if len(parts) == 1:
            self.action_choose_mode()
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

    async def _cmd_tasks(self: LumenApp, parts: list[str], raw: str) -> None:
        self.action_toggle_plan()

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

        bootstrap = await self.workspace_host.dispatch(GetBootstrap())
        mcp_rows = cast(Any, bootstrap).mcp
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

        response = await self.workspace_host.dispatch(ListHooks())
        hooks = cast(list[dict[str, Any]], cast(Any, response).data.get("items", []))
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

        response = await self.workspace_host.dispatch(
            ListMcpResources(self.session.id if self.session is not None else None)
        )
        resources = cast(list[dict[str, Any]], cast(Any, response).data.get("items", []))
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
                result = await self.workspace_host.dispatch(
                    SetContextSource(self.session.id, "resource", parts[1], True)
                )
            except Exception as error:
                await self._append_system(f"Cannot load MCP resource: {error}")
            else:
                await self._append_system(
                    f"Loaded {cast(Any, result).data['reference']} into retrieved context."
                )
            return
        if len(parts) == 3 and parts[1] == "refresh":
            if self.session is None:
                await self._append_system("No active session.")
                return
            try:
                result = await self.workspace_host.dispatch(
                    SetContextSource(self.session.id, "resource", parts[2], True)
                )
            except Exception as error:
                await self._append_system(f"Cannot refresh MCP resource: {error}")
            else:
                await self._append_system(
                    f"Refreshed {cast(Any, result).data['reference']} for this session."
                )
            return
        if len(parts) == 3 and parts[1] == "unload":
            if self.session is None:
                await self._append_system("No active session.")
                return
            await self.workspace_host.dispatch(
                SetContextSource(self.session.id, "resource", parts[2], False)
            )
            await self._append_system(f"Unloaded MCP resource {parts[2]!r} from this session.")
            return
        await self._unknown_command(parts[0].lower())

    async def _cmd_prompts(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/prompts`` — list MCP prompt templates."""

        response = await self.workspace_host.dispatch(ListMcpPrompts())
        prompts = cast(list[dict[str, Any]], cast(Any, response).data.get("items", []))
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
        """``/context [--json|sources|capabilities]`` — render context diagnostics."""

        await self._render_context(parts)

    async def _cmd_instructions(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/instructions [--json]`` — inspect prompt mode and provenance."""

        response = await self.workspace_host.dispatch(GetInstructions())
        data = cast(dict[str, Any], cast(Any, response).data)
        if "--json" in parts[1:]:
            await self._append_system(json.dumps(data, ensure_ascii=False, indent=2))
            return
        sources = cast(list[dict[str, Any]], data.get("sources", []))
        lines = [
            f"模式: {data.get('mode')}  preset: {data.get('preset') or '-'}  版本: {data.get('version')}",
            (
                f"稳定指令: {data.get('characters', 0)} 字符  "
                f"动态上下文: {data.get('runtime_context_characters', 0)} 字符"
            ),
            f"模型: {data.get('active_model')} ({data.get('model_id')})",
            "来源:",
        ]
        lines.extend(
            f"- {row.get('role')} · {row.get('origin')} · {row.get('revision')}"
            for row in sources
        )
        await self._append_system("\n".join(lines))

    async def _cmd_compact(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/compact [focus]`` — force a compaction."""

        focus = shlex.join(parts[1:]) if len(parts) > 1 else None
        await self._run_context_control("compact", focus=focus)

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
        await self._run_context_control("memory", action=action, payload=payload)

    async def _cmd_retry(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/retry`` — re-send the last prompt."""

        if self.last_prompt is None:
            await self._append_system("There is no previous prompt to retry.")
        else:
            await self.handle_input(self.last_prompt, is_retry=True)

    async def _cmd_edit(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/edit`` — edit the last prompt in $EDITOR and resend on a fresh branch.

        The branch keeps every turn before the edited one, so the original
        exchange stays intact in the source session. Workspace files are left
        unchanged, matching /checkpoints rewind semantics.
        """

        if self.last_prompt is None:
            await self._append_system("There is no previous prompt to edit.")
            return
        edited = await edit_text_external(self.last_prompt, suffix=".md")
        if edited is None:
            self.notify("Set $VISUAL or $EDITOR to edit the prompt", severity="warning")
            return
        edited = edited.strip()
        if not edited or edited == self.last_prompt.strip():
            self.notify("Prompt unchanged", timeout=2)
            return

        checkpoints = await self.coordinator.list_checkpoints()
        last_index = max((int(item["index"]) for item in checkpoints), default=-1)
        if last_index < 0:
            await self._append_system("There is no previous turn to branch from.")
            return
        if last_index == 0:
            # Nothing precedes the edited turn; start from a fresh session.
            state = await self.coordinator.new_session()
            await self._apply_coordinator_state(state, restored=True)
            await self._append_system(f"Started fresh session {state.session.id} for the edited prompt.")
        else:
            try:
                session_id = await self.coordinator.fork_at_checkpoint(last_index - 1)
            except Exception as error:
                await self._append_system(f"Cannot branch for /edit: {error}")
                return
            await self._resume_checkpoint_branch(session_id)
        self._refresh_topbar()
        await self.handle_input(edited)

    async def _cmd_exit(self: LumenApp, parts: list[str], raw: str) -> None:
        """``/exit`` (alias ``/quit``) — exit the app."""

        self.exit()
