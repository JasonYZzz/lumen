import { describe, expect, it } from 'vitest'
import type { PlanState, PlanStep } from './api/types'
import { planPresentation } from './plan-presentation'

function plan(statuses: PlanStep['status'][], lifecycle = 'draft'): PlanState {
  return { goal: '', revision: 1, state_version: 1, lifecycle, approved_revision: null,
    steps: statuses.map((status, index) => ({ id: String(index), title: `Step ${index}`, status, depends_on: [], acceptance_criteria: [] })) }
}

describe('plan presentation', () => {
  it('keeps skipped steps distinct from successful work', () => {
    expect(planPresentation(plan(['completed', 'skipped']))).toMatchObject({ completed: 1, skipped: 1, settled: true, status: '步骤已结束' })
  })
  it('puts a blocker ahead of concurrent active work', () => {
    expect(planPresentation(plan(['in_progress', 'blocked', 'pending'])).current?.status).toBe('blocked')
  })
  it('does not infer approval from completion counts', () => {
    expect(planPresentation(plan(['completed'], 'review_pending')).status).toBe('待确认')
  })
  it('does not mark empty or cancelled plans as completed', () => {
    expect(planPresentation(plan([]))).toMatchObject({ settled: false, summary: '尚未生成步骤' })
    expect(planPresentation(plan(['completed'], 'cancelled')).status).toBe('已取消')
  })
})
