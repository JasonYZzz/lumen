import type { PlanState, PlanStep } from './api/types'

export const stepStatusLabel: Record<PlanStep['status'], string> = {
  pending: '待开始',
  in_progress: '进行中',
  completed: '已完成',
  blocked: '受阻',
  skipped: '已跳过',
}

/** A read-only projection. Counts never grant approval or declare run completion. */
export function planPresentation(plan: PlanState) {
  const completed = plan.steps.filter((step) => step.status === 'completed').length
  const skipped = plan.steps.filter((step) => step.status === 'skipped').length
  const blocked = plan.steps.find((step) => step.status === 'blocked')
  const current = blocked ?? plan.steps.find((step) => step.status === 'in_progress')
    ?? plan.steps.find((step) => step.status === 'pending')
  const settled = plan.steps.length > 0 && completed + skipped === plan.steps.length
  const status = plan.lifecycle === 'review_pending' ? '待确认'
    : plan.lifecycle === 'rejected' ? '待调整'
    : plan.lifecycle === 'cancelled' ? '已取消'
    : blocked || plan.lifecycle === 'blocked' ? '受阻'
    : settled ? (skipped ? '步骤已结束' : '步骤已完成')
    : '任务计划'
  const summary = current
    ? `${stepStatusLabel[current.status]} · ${current.title}`
    : plan.steps.length ? (skipped ? `${skipped} 项已跳过` : '所有步骤已完成') : '尚未生成步骤'
  return { completed, skipped, current, settled, status, summary }
}
