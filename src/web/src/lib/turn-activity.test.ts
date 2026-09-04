import { describe, expect, it } from 'vitest'
import type { TimelineEntry } from './api/types'
import {
  activityMeta,
  activityDuration,
  activityTitle,
  transcriptEventLabel,
  timelineNoteLabel,
  toolSourceLabel,
  turnPresentation,
} from './turn-activity'

function entry(kind: TimelineEntry['kind'], overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  return { id: `${kind}-entry`, kind, text: '', ...overrides }
}

describe('turn activity presentation', () => {
  it('classifies interim text as soon as a tool starts, before a final answer exists', () => {
    const interim = entry('assistant', { text: 'Let me check.' })
    const presentation = turnPresentation([interim, entry('tool', { status: 'running' })])
    expect(presentation.foreground).toEqual([])
    expect(presentation.activity.map((item) => item.kind)).toEqual(['commentary', 'tool'])
  })

  it('formats real elapsed measurements without inventing missing or invalid values', () => {
    expect(activityDuration(undefined)).toBeNull()
    expect(activityDuration(Number.NaN)).toBeNull()
    expect(activityDuration(Infinity)).toBeNull()
    expect(activityDuration(-1)).toBeNull()
    expect(activityDuration(0.4)).toBe('不足 1s')
    expect(activityDuration(155.9)).toBe('2m 35s')
    expect(activityDuration(60)).toBe('1m')
    expect(activityDuration(3605)).toBe('1h 5s')
  })
  it('folds interim answers followed by tool work without changing stored entries', () => {
    const interim = entry('assistant', { id: 'interim', text: 'I will inspect the files.' })
    const final = entry('assistant', { id: 'final', text: 'Here is the result.' })
    const presentation = turnPresentation([interim, entry('tool'), final])
    expect(presentation.foreground).toEqual([final])
    expect(presentation.activity[0]).toMatchObject({ id: 'interim', kind: 'commentary' })
    expect(interim.kind).toBe('assistant')
    expect(turnPresentation([interim, final]).foreground).toEqual([interim, final])
  })
  it('keeps recovered tool errors in the audit summary without an open confirmation', () => {
    const presentation = turnPresentation([
      entry('thinking', { text: 'working' }),
      entry('tool', { isError: true, status: 'completed' }),
      entry('tool', { status: 'denied' }),
      entry('tool', { status: 'completed' }),
      entry('assistant', { text: 'Recovered and finished' }),
    ])
    expect(presentation.requiresAttention).toBe(false)
    expect(activityTitle(presentation, false)).toBe('已完成处理')
    expect(activityMeta(presentation)).toContain('2 次失败记录')
  })

  it('does not mislabel a terminal error as completed', () => {
    const presentation = turnPresentation([entry('thinking'), entry('error', { text: 'Run failed', status: 'failed' })])
    expect(presentation.requiresAttention).toBe(false)
    expect(activityTitle(presentation, false)).toBe('处理未完成')
    expect(presentation.foreground[0].kind).toBe('error')
  })

  it('distinguishes recovered compaction errors, cancellation and blocking questions', () => {
    expect(activityTitle(turnPresentation([entry('error', { text: 'Summary failed; using window' })]), false))
      .toBe('已完成处理')
    for (const status of ['cancelled', 'interrupted']) {
      expect(activityTitle(turnPresentation([entry('system', { status })]), false)).toBe('处理已停止')
    }
    expect(activityTitle(turnPresentation([entry('system', { status: 'waiting_for_user' })]), false))
      .toBe('等待补充信息')
  })

  it('renders system notes without a redundant role label', () => {
    expect(timelineNoteLabel('system')).toBe('')
    expect(timelineNoteLabel('error')).toBe('错误')
    expect(timelineNoteLabel('agent')).toBe('Agent')
  })

  it('uses readable transcript labels without exposing internal role names', () => {
    expect(transcriptEventLabel(entry('system'))).toBe('事件')
    expect(transcriptEventLabel(entry('thinking'))).toBe('思考')
    expect(transcriptEventLabel(entry('tool', { callView: { family: 'mcp' } }))).toBe('MCP')
  })

  it('omits plan controls from conversation history while keeping the final answer visible', () => {
    const presentation = turnPresentation([
      entry('thinking', { text: 'Inspect the repository' }),
      entry('tool', { toolName: 'read_file', status: 'completed' }),
      entry('plan', { plan: { goal: '', revision: 1, state_version: 1, lifecycle: 'draft', approved_revision: null, steps: [] } }),
      entry('assistant', { text: 'Done' }),
    ])

    expect(presentation.activity.map((item) => item.kind)).toEqual(['thinking', 'tool'])
    expect(presentation.foreground.map((item) => item.kind)).toEqual(['assistant'])
    expect(activityTitle(presentation, false)).toBe('已完成处理')
    expect(activityMeta(presentation)).toBe('2 项活动 · 1 次工具调用')
  })

  it('hides plan history without rewriting the recorded updates', () => {
    const earlier = entry('plan', { id: 'plan-1', text: '最初计划' })
    const latest = entry('plan', { id: 'plan-2', text: '最新计划' })
    const recorded = [earlier, entry('commentary', { text: '继续处理' }), latest, entry('assistant')]
    const result = turnPresentation(recorded)
    expect(result.activity.map((item) => item.id)).toEqual(['commentary-entry'])
    expect(recorded).toHaveLength(4)
    expect(recorded[0]).toBe(earlier)
  })

  it('identifies Skill and MCP activity and keeps approvals prominent', () => {
    const presentation = turnPresentation([
      entry('tool', {
        pendingApproval: true,
        callView: { family: 'mcp' },
      }),
      entry('tool', { callView: { family: 'skill' }, status: 'completed' }),
    ], '/skill:review-pr 42')

    expect(presentation).toMatchObject({
      mcpCount: 1,
      skillCount: 1,
      skillName: 'review-pr',
      requiresAttention: true,
    })
    expect(activityTitle(presentation, false)).toBe('处理过程需要确认')
    expect(activityMeta(presentation)).toBe('2 项活动 · 2 次工具调用 · MCP 1 · Skill 1')
    expect(toolSourceLabel(presentation.activity[0])).toBe('MCP')
  })
})
