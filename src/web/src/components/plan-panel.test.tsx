import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { PlanPanel, PlanProgress } from './plan-panel'
import { MarkdownMessage } from './markdown-message'
import type { PlanState } from '@/lib/api/types'

;(globalThis as typeof globalThis & { React: typeof React }).React = React

describe('plan and output reading surface', () => {
  it('labels completion count separately from the current step, including parallel work', () => {
    const plan: PlanState = { goal: 'Report', revision: 1, state_version: 3, lifecycle: 'executing', approved_revision: null,
      steps: [
        { id: 'research', title: 'Research', status: 'in_progress', depends_on: [], acceptance_criteria: [] },
        { id: 'structure', title: 'Structure', status: 'completed', depends_on: [], acceptance_criteria: [] },
        { id: 'write', title: 'Write', status: 'pending', depends_on: [], acceptance_criteria: [] },
      ] }
    const markup = renderToStaticMarkup(<PlanProgress plan={plan} running />)
    expect(markup).toContain('已完成 1 / 3')
    expect(markup).toContain('Research')
    expect(markup).not.toContain('步骤 1 / 3')
  })
  it('puts explanations in disclosure controls and labels skipped status', () => {
    const plan: PlanState = { goal: 'Deliver the UI', revision: 2, state_version: 3, lifecycle: 'executing', approved_revision: 2,
      steps: [{ id: 'one', title: '<script>literal title</script>', status: 'skipped', note: 'Optional validation was not run', depends_on: [], acceptance_criteria: [{ id: 'check', description: 'Keyboard support' }] }] }
    const markup = renderToStaticMarkup(<PlanPanel plan={plan} />)
    expect(markup).toContain('<details class="plan-step-details">')
    expect(markup).toContain('已跳过')
    expect(markup).toContain('验收条件：')
    expect(markup).not.toContain('<script>')
    expect(markup).not.toContain('class="spin"')
  })
  it('adds code copying without interpreting raw HTML', () => {
    const markup = renderToStaticMarkup(<MarkdownMessage content={'```python\nprint(1)\n```\n\n<script>alert(1)</script>'} />)
    expect(markup).toContain('aria-label="复制代码"')
    expect(markup).toContain('print(1)')
    expect(markup).not.toContain('<script>')
  })
})
