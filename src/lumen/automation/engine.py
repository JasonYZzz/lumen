# User-visible Chinese copy is authored in its native punctuation.
# ruff: noqa: RUF001
"""Timers admit durable intentions; WorkspaceHost remains the only run authority."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lumen.context.artifacts import ArtifactStore
from lumen.context.memory.redaction import contains_secret
from lumen.io import run_io

from .models import (
    AssistantEdit,
    AssistantState,
    AutomationCommand,
    ContextRef,
    Definition,
    Idea,
    Occurrence,
    Phase,
    Result,
)
from .store import AssistantConflict, AutomationStore, digest

if TYPE_CHECKING:
    from lumen.application.run_lock import WorkspaceRunLock


class AutomationEngine:
    def __init__(
        self,
        store: AutomationStore,
        owner_lock: WorkspaceRunLock,
        artifacts: ArtifactStore,
        *,
        execute: Callable[[Definition, Occurrence], Awaitable[Occurrence | None]],
        recover: Callable[[Definition, Occurrence], Awaitable[Occurrence]],
        fingerprint: Callable[[Definition], str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self._owner_lock = owner_lock
        self.artifacts = artifacts
        self._execute = execute
        self._recover = recover
        self._fingerprint = fingerprint
        self.clock = clock
        self.owner = False
        self.status = "stopped"
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def open(self, *, owner: bool = True) -> None:
        try:
            await self._open(owner=owner)
        except (sqlite3.Error, OSError, ValueError):
            await self.close()
            self.status = "storage_error"

    async def _open(self, *, owner: bool) -> None:
        await run_io(self.store.open)
        self.owner = owner and await run_io(self._owner_lock.acquire)
        self.status = "running" if self.owner else "another_host" if owner else "read_only"
        if self.owner:
            self._wake = asyncio.Event()
            # Reconcile before a timer can materialize a new edition.
            while True:
                recovering = [
                    item for item in await run_io(self.store.pending) if item.phase is not Phase.QUEUED
                ]
                if not recovering:
                    break
                for occurrence in recovering:
                    definition = await run_io(
                        self.store.get, "definition", occurrence.automation_id, Definition
                    )
                    restored = await self._recover(definition, occurrence)
                    await self._settle(restored)
            self._task = asyncio.create_task(self._loop(), name="lumen-automation")

    async def close(self) -> None:
        if self._task is not None:
            self.status = "stopping"
            self._wake.set()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await run_io(self.store.close)
        if self.owner:
            await run_io(self._owner_lock.release)
        self.owner = False
        self.status = "stopped"

    def require_owner(self) -> None:
        if not self.owner or self.status == "storage_error":
            raise AssistantConflict("Automation owner is unavailable; connect to its running Host")

    async def command(self, command: AutomationCommand) -> str:
        self.require_owner()
        fingerprint = ""
        definition = None
        if command.id is not None:
            definition = await run_io(self.store.get, "definition", command.id, Definition)
            if command.action == "update" and command.spec is not None:
                definition = definition.model_copy(update={"spec": command.spec})
            fingerprint = await run_io(self._fingerprint, definition)
        if (
            sys.platform == "win32"
            and command.action in {"trial", "run", "enable"}
            and definition is not None
            and definition.spec.profile == "workspace_snapshot"
        ):
            raise ValueError("Secure workspace automation reads require macOS or Linux")
        result = await run_io(self.store.command, command, fingerprint, self.clock())
        self._wake.set()
        return result

    async def edit(self, edit: AssistantEdit) -> None:
        self.require_owner()
        if contains_secret(edit.content) or contains_secret(edit.title):
            raise ValueError("Do not store credentials or personal identifiers in assistant resources")
        reference = (
            await self.artifacts.astore(edit.content) if edit.action in {"note", "edit_resource"} else None
        )
        await run_io(self.store.edit, edit, artifact_ref=reference)

    async def state(self) -> AssistantState:
        ideas = (
            Idea(
                id="weekly-review",
                title="让每周回顾更轻松",
                category="work",
                reason="把选定资料中的进展、风险和下一步整理成一份可回看的周报。",
                intent="读取用户选定的工作区资料，整理有来源依据的本周进展、风险、待确认事项与下一步。"
                "只总结实际可核实的信息；生成一份 Markdown 周报文件并提供文件链接。不创建或启用定时任务。",
                outcome="一份含来源、进展和下一步的周报文件",
                steps=("核对选定资料与时间范围", "提取变化和待确认事项", "生成周报并标明来源"),
                input_hint="要回顾的工作区文件或目录，以及时间范围",
            ),
            Idea(
                id="reading-notes",
                title="把阅读变成可以回顾的笔记",
                category="learning",
                reason="资源库可以保存笔记，稍后继续讨论。",
                intent="读取用户指定的工作区材料，整理关键观点、实际出处与思考问题，"
                "生成一份 Markdown 阅读笔记文件并提供链接。缺少材料时询问，不编造阅读结果。",
                outcome="一份含关键观点、出处和思考问题的阅读笔记文件",
                steps=("阅读指定材料", "归纳关键观点并标明出处", "生成可回顾的笔记文件"),
                input_hint="要阅读的工作区文件，以及你关心的问题",
            ),
            Idea(
                id="personal-goal",
                title="为一件想做的事留出位置",
                category="life",
                reason="明确的里程碑比笼统的完成率更容易推进。",
                intent="根据用户描述的目标和约束，提出成功标准、可验收的里程碑和最小下一步。"
                "生成一份可讨论的行动方案文件，不擅自保存目标、标记完成或启用自动化。",
                outcome="一份包含成功标准、里程碑与第一步的行动方案",
                steps=("澄清希望达成的结果", "拆解可验收的里程碑", "明确最小下一步和所需条件"),
                input_hint="想达成什么，有哪些时间或资源约束",
            ),
        )
        try:
            state = await run_io(self.store.snapshot, self.owner, self.status, ideas)
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            self.status = "storage_error"
            state = AssistantState(owner=self.owner, status=self.status)
        contextual: list[Idea] = []
        draft = next((item for item in state.definitions if item.validation is None), None)
        if draft is not None:
            contextual.append(
                Idea(
                    id=f"draft:{draft.id}:{draft.revision}",
                    title=f"先检查「{draft.spec.name}」的范围",
                    category="work",
                    reason="这份自动化还没有通过当前配置的试跑。",
                    intent=f"请检查自动化 {draft.id} 的来源、时间和预算，和我确认后再试跑，不自动启用。",
                    source="context", source_label=f"来自持续跟进 · {draft.spec.name}",
                    outcome="在原安排中检查范围并试跑", destination="automation", target_id=draft.id,
                )
            )
        for goal in state.goals:
            if len(contextual) >= 4:
                break
            if goal.deleted_at or goal.status != "active":
                continue
            if goal.execution and goal.execution.status == "active":
                continue
            if goal.execution and goal.execution.status == "review":
                contextual.append(Idea(
                    id=f"goal-review:{goal.id}:{goal.revision}", title=f"验收「{goal.title}」的成果",
                    category="work", source="context", source_label=f"来自目标 · {goal.title}",
                    reason="推进已提交核验记录，等待你确认是否达到成功标准。",
                    intent="", outcome="核对成功标准与实际成果，决定是否完成目标",
                    destination="goal", target_id=goal.id,
                ))
                continue
            next_step = next((item for item in goal.milestones if not item.done), None)
            contextual.append(
                    Idea(
                        id=f"goal:{digest([goal.id, goal.revision])[:32]}",
                        title=f"继续推进「{goal.title}」",
                        category="work", source="context", source_label=f"来自目标 · {goal.title}",
                        context_refs=(ContextRef(kind="goal", id=goal.id, revision=goal.revision),),
                        reason=f"下一项未完成的里程碑：{next_step.title}" if next_step
                        else "这个目标还没有明确的下一步，可以先核对现状和成功标准。",
                        intent=f"围绕目标「{goal.title}」"
                        + (f"的里程碑「{next_step.title}」" if next_step else "")
                        + "，结合关联对话、已有成果和参考资料，核对现状并完成一个可验证的最小下一步。"
                        "缺少关键条件时明确提出问题；提供实际结果及核验依据，不擅自标记里程碑或目标完成。",
                        outcome="一个可核验的阶段成果，或明确的阻塞与下一步",
                        steps=("核对目标、原对话与已有资料", "完成一个最小可验证步骤", "返回成果和核验依据"),
                    )
                )
            if len(contextual) >= 4:
                break
        for resource in state.resources:
            if len(contextual) >= 6:
                break
            if resource.deleted_at or not resource.published or resource.status != "active":
                continue
            if resource.file_type != "document" and resource.kind not in {"note", "report"}:
                continue
            contextual.append(Idea(
                id=f"resource:{resource.id}:{resource.revision}", title=f"把「{resource.title}」变成行动清单",
                category="learning", source="context", source_label=f"来自资料 · {resource.title}",
                reason="这份已有资料可以继续使用，从内容中提炼可落实的下一步。",
                context_refs=(ContextRef(kind="resource", id=resource.id, revision=resource.revision),),
                intent="阅读关联资料，提炼关键结论、可执行事项和待确认问题，引用实际出处。"
                "生成一份新的 Markdown 行动清单文件并给出链接；不覆盖原资料，不编造日期、负责人或完成状态。",
                outcome="一份有出处的行动清单文件",
                steps=("阅读当前版本的资料", "提取结论和可执行事项", "生成独立清单，保留原资料"),
            ))
        for index, idea in enumerate(contextual):
            contextual[index] = idea.model_copy(
                update={
                    "dismissed": await run_io(self.store.idea_dismissed, idea.id),
                }
            )
        return state.model_copy(
            update={
                "workspace_source_available": sys.platform != "win32",
                "ideas": (*contextual, *state.ideas),
            }
        )

    async def _settle(self, occurrence: Occurrence) -> None:
        text = ""
        if occurrence.artifact_ref is not None:
            body = await run_io(self.artifacts.read, occurrence.artifact_ref)
            if body is None:
                occurrence = occurrence.model_copy(
                    update={
                        "artifact_ref": None,
                        "result": Result.FAILED,
                        "detail": "Result artifact is unavailable",
                    }
                )
            else:
                text = body.decode("utf-8")[:100_000]
        candidate = occurrence.model_copy(update={"phase": Phase.SETTLING})
        await run_io(self.store.put_occurrence, candidate)
        await run_io(self.store.settle, candidate, text)

    async def tick(self) -> None:
        self.require_owner()
        await run_io(self.store.materialize, self.clock())
        for occurrence in await run_io(self.store.pending):
            definition = await run_io(self.store.get, "definition", occurrence.automation_id, Definition)
            if occurrence.phase is Phase.SETTLING:
                await self._settle(occurrence)
                continue
            if occurrence.phase is Phase.RUNNING:
                await self._settle(await self._recover(definition, occurrence))
                continue
            try:
                result = await self._execute(definition, occurrence)
                if result is None:  # Host is busy; this is not a model retry.
                    return
            except AssistantConflict as error:
                result = occurrence.model_copy(
                    update={
                        "result": Result.NEEDS_USER_ACTION,
                        "detail": str(error),
                    }
                )
            await self._settle(result)
            # Yield to new foreground requests before admitting another background run.
            await asyncio.sleep(0)

    async def _loop(self) -> None:
        while self.status == "running":
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fail closed on storage/recovery faults; never recreate a database or replay a run.
                self.status = "storage_error"
                return
            try:
                async with asyncio.timeout(1):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()
