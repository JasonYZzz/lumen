"""Per-run task controller exposing public planning control tools."""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, StrictStr

from lumen.events import PlanCreated, PlanUpdated, ProgressReported, RunEvent
from lumen.plan import (
    EvidenceReceipt,
    PlanLifecycle,
    PlanState,
    PlanStep,
    PlanStepInput,
    StepStatus,
)

EventSink = Callable[[RunEvent], None] | Callable[[RunEvent], Awaitable[None]]
_PROGRESS_MAX_CHARS = 800


def _decode_string_list(value: object) -> object:
    """Normalize the narrow provider quirk that JSON-encodes flat string arrays."""

    if isinstance(value, str):
        if len(value) > 8_192:
            raise ValueError("字符串数组 JSON 过长")
        try:
            return json.loads(value)
        except ValueError as error:
            raise ValueError("参数必须是字符串数组") from error
    return value


ModelStringList = Annotated[
    list[StrictStr], BeforeValidator(_decode_string_list),
]

#: The plan/clarification control tools owned by :class:`TaskController`. This
#: is the single authority for "which tool names are parent-loop control
#: plane": name reservation, timeline filtering, and child-progress forwarding
#: all consult this set.
CONTROL_TOOL_NAMES = frozenset(
    {
        "set_plan",
        "update_step",
        "link_evidence",
        "report_progress",
        "request_clarification",
    }
)


class TaskController:
    def __init__(self) -> None:
        self._state = PlanState()
        self._sink: EventSink | None = None
        self._plan_updated = False

    def start(self, plan: PlanState, sink: EventSink) -> None:
        self._state = deepcopy(plan)
        self._sink = sink
        self._plan_updated = False

    @property
    def plan_updated(self) -> bool:
        """Whether this run claimed/updated the plan, not just inherited it."""
        return self._plan_updated

    def snapshot(self) -> PlanState:
        return deepcopy(self._state)

    async def _emit(self, event: RunEvent) -> None:
        if isinstance(event, (PlanCreated, PlanUpdated)):
            self._plan_updated = True
        if self._sink is None:
            return
        result: Any = self._sink(event)
        if inspect.isawaitable(result):
            await result

    async def set_plan(
        self,
        steps: list[PlanStepInput],
        goal: str = "",
        constraints: ModelStringList | None = None,
    ) -> str:
        """定义或修订当前任务的计划，不用于汇报进度。

        未变化的步骤应保持稳定 ID，使其状态和证据能跨修订保留。每个步骤开始或结束时立即
        调用 update_step。若是无关的新任务，请提供新 goal 和新步骤 ID，并省略旧任务步骤。
        参数已知时，可以在同一个有序响应中建立计划、启动第一步并调用工作工具，无需浪费
        单独的规划轮次。

        Args:
            steps: 简短里程碑。普通进度和纯推理准备步骤应让 acceptance_criteria 为空；
                设置验收标准后，完成前必须有具体执行回执。
            goal: 任务目标，执行期间应保持稳定。
            constraints: 可选的 JSON 字符串数组，不要传入经过 JSON 编码的字符串。
        """
        if self._sink is None:
            raise RuntimeError("TaskController.start() 之前不能调用 set_plan")
        resolved_goal = goal.strip() or self._state.goal
        resolved_constraints = list(self._state.constraints if constraints is None else constraints)
        same_task = resolved_goal == self._state.goal and resolved_constraints == self._state.constraints
        previous = {step.id: step for step in self._state.steps}
        preserved = {
            step.id
            for step in steps
            if same_task
            and step.id in previous
            and step.model_dump() == previous[step.id].model_dump(include=set(PlanStepInput.model_fields))
        }
        # Changed prerequisites invalidate dependent execution state as well.
        while invalid := {
            step.id for step in steps if step.id in preserved and set(step.depends_on) - preserved
        }:
            preserved -= invalid
        new_steps = [
            previous[step.id].model_copy(deep=True)
            if step.id in preserved
            else PlanStep.model_validate(step.model_dump())
            for step in steps
        ]
        if same_task and new_steps == self._state.steps:
            await self._emit(PlanUpdated(self.snapshot()))
            return "计划没有变化；请用 update_step 更新进度。"
        previous_revision = self._state.revision
        self._state = PlanState(
            goal=resolved_goal,
            constraints=resolved_constraints,
            revision=previous_revision + 1,
            state_version=self._state.state_version + 1,
            lifecycle=PlanLifecycle.DRAFT,
            steps=new_steps,
            evidence=self._state.evidence if same_task else [],
        )
        event_type: type[RunEvent] = PlanCreated if previous_revision == 0 else PlanUpdated
        await self._emit(event_type(self.snapshot()))
        return "计划已更新。"

    async def update_step(
        self,
        step_id: str,
        status: StepStatus,
        note: str | None = None,
        owner: str | None = None,
    ) -> str:
        """步骤开始、完成或受阻时立即更新它。

        不要把所有更新拖到最终回答。已完成步骤保持完成；遇到阻塞时应报告阻塞，只有理由充分
        时才跳过，不得编造成功。存在验收标准时先调用 link_evidence；可以在同一响应中关联
        证据、完成当前步骤并启动下一步。
        """
        if self._sink is None:
            raise RuntimeError("TaskController.start() 之前不能调用 update_step")
        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"未知步骤 ID: {step_id}")
        current = self._state.steps[index]
        if current.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
            raise ValueError(f"已结束的步骤 {step_id!r} 不能更改状态")
        if status in {StepStatus.IN_PROGRESS, StepStatus.COMPLETED}:
            statuses = {step.id: step.status for step in self._state.steps}
            incomplete = [
                dependency
                for dependency in current.depends_on
                if statuses[dependency] not in {StepStatus.COMPLETED, StepStatus.SKIPPED}
            ]
            if incomplete:
                raise ValueError(f"步骤 {step_id!r} 存在未完成的依赖: {incomplete}")
        if status is StepStatus.COMPLETED:
            receipts = {receipt.id: receipt for receipt in self._state.evidence}
            covered = {
                criterion_id
                for evidence_id in current.evidence_ids
                if (receipt := receipts.get(evidence_id)) is not None and receipt.passed
                for criterion_id in receipt.criterion_ids
            }
            missing = [
                criterion.id
                for criterion in current.acceptance_criteria
                if criterion.id not in covered
            ]
            if missing:
                available = [
                    {"id": receipt.id, "summary": receipt.summary[:160]}
                    for receipt in self._state.evidence if receipt.passed
                ][-8:]
                raise ValueError(
                    "; ".join(
                        f"验收标准 {criterion_id} 没有关联通过的证据"
                        for criterion_id in missing
                    )
                    + f"。完成步骤前请调用 link_evidence(step_id={step_id!r}, evidence_id=..., "
                    f"criterion_ids={missing!r})。最近可用的通过回执: {available!r}。"
                    "若没有回执能支持这些标准，请运行所需验证；不要删除验收标准。"
                )
        updated = current.model_copy(update={"status": status, "note": note, "owner": owner})
        new_steps = list(self._state.steps)
        new_steps[index] = updated
        lifecycle = PlanLifecycle.EXECUTING
        if any(step.status is StepStatus.BLOCKED for step in new_steps):
            lifecycle = PlanLifecycle.BLOCKED
        elif new_steps and all(
            step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED} for step in new_steps
        ):
            lifecycle = PlanLifecycle.COMPLETED
        self._state = self._state.model_copy(
            update={
                "steps": new_steps,
                "state_version": self._state.state_version + 1,
                "lifecycle": lifecycle,
            }
        )
        await self._emit(PlanUpdated(self.snapshot()))
        return "步骤已更新。"

    def record_evidence(self, receipt: EvidenceReceipt) -> None:
        if any(item.id == receipt.id for item in self._state.evidence):
            raise ValueError(f"duplicate evidence receipt: {receipt.id}")
        self._state = self._state.model_copy(
            update={
                "evidence": [*self._state.evidence, receipt],
                "state_version": self._state.state_version + 1,
            }
        )

    async def link_evidence(
        self,
        step_id: str,
        evidence_id: str,
        criterion_ids: ModelStringList,
    ) -> str:
        """把执行器产生的 plan_evidence 回执关联到本步骤的验收标准。

        使用工具结果中的回执 ID，不要使用工作对象 ID 或 artifact hash。成功的工具回执只能
        证明实际执行；仅将它关联到其结果确实支持的验收标准。
        """
        if self._sink is None:
            raise RuntimeError("TaskController.start() 之前不能调用 link_evidence")
        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"未知步骤 ID: {step_id}")
        receipt = next((item for item in self._state.evidence if item.id == evidence_id), None)
        if receipt is None:
            raise ValueError(f"未知证据回执: {evidence_id}")
        valid = {item.id for item in self._state.steps[index].acceptance_criteria}
        requested = set(criterion_ids)
        if not requested or not requested <= valid:
            raise ValueError("criterion_ids 必须引用所选步骤上的验收标准")
        receipts = list(self._state.evidence)
        receipt_index = next(index for index, item in enumerate(receipts) if item.id == evidence_id)
        receipts[receipt_index] = receipt.model_copy(
            update={"criterion_ids": list(dict.fromkeys([*receipt.criterion_ids, *criterion_ids]))}
        )
        step = self._state.steps[index]
        evidence_ids = list(dict.fromkeys([*step.evidence_ids, evidence_id]))
        steps = list(self._state.steps)
        steps[index] = step.model_copy(update={"evidence_ids": evidence_ids})
        self._state = self._state.model_copy(
            update={
                "steps": steps,
                "evidence": receipts,
                "state_version": self._state.state_version + 1,
            }
        )
        await self._emit(PlanUpdated(self.snapshot()))
        return "证据已关联。"

    async def attach_executor_evidence(
        self,
        step_id: str,
        evidence_id: str,
        criterion_ids: list[str] | None = None,
    ) -> None:
        """Attach trusted executor evidence, optionally covering criteria.

        This internal Interface is used by runtimes such as AgentOrchestrator;
        model-visible callers continue to use ``link_evidence``.
        """

        requested = list(criterion_ids or [])
        if requested:
            await self.link_evidence(step_id, evidence_id, requested)
            return
        index = self._find_step_index(step_id)
        if index is None:
            raise ValueError(f"unknown step id: {step_id}")
        if not any(item.id == evidence_id for item in self._state.evidence):
            raise ValueError(f"unknown evidence receipt: {evidence_id}")
        step = self._state.steps[index]
        steps = list(self._state.steps)
        steps[index] = step.model_copy(
            update={"evidence_ids": list(dict.fromkeys([*step.evidence_ids, evidence_id]))}
        )
        self._state = self._state.model_copy(
            update={"steps": steps, "state_version": self._state.state_version + 1}
        )
        await self._emit(PlanUpdated(self.snapshot()))

    async def report_progress(
        self,
        summary: Annotated[str, Field(min_length=1, max_length=_PROGRESS_MAX_CHARS)],
        next_action: str | None = None,
    ) -> str:
        """在对用户有帮助时报告一条简短的公开进度。

        工作调用已准备好时，优先在同一响应中附带简短说明，不要为本工具浪费单独的模型轮次。
        """
        if self._sink is None:
            raise RuntimeError("TaskController.start() 之前不能调用 report_progress")
        stripped = summary.strip()
        if not stripped:
            raise ValueError("进度摘要不能为空")
        if len(summary) > _PROGRESS_MAX_CHARS:
            raise ValueError(f"进度摘要超过 {_PROGRESS_MAX_CHARS} 个字符")
        await self._emit(ProgressReported(summary=summary, next_action=next_action))
        return "进度已报告。"

    def _find_step_index(self, step_id: str) -> int | None:
        return next((index for index, step in enumerate(self._state.steps) if step.id == step_id), None)
