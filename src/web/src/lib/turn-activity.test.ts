import { describe, expect, it } from 'vitest'
import type { TimelineEntry } from './api/types'
import {
  activityMeta,
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

  it('moves process events behind one disclosure while keeping the answer and plan visible', () => {
    const presentation = turnPresentation([
      entry('thinking', { text: 'Inspect the repository' }),
      entry('tool', { toolName: 'read_file', status: 'completed' }),
      entry('plan', { plan: { goal: '', revision: 1, state_version: 1, lifecycle: 'draft', approved_revision: null, steps: [] } }),
      entry('assistant', { text: 'Done' }),
    ])

    expect(presentation.activity.map((item) => item.kind)).toEqual(['thinking', 'tool'])
    expect(presentation.foreground.map((item) => item.kind)).toEqual(['plan', 'assistant'])
    expect(activityTitle(presentation, false)).toBe('已完成处理')
    expect(activityMeta(presentation)).toBe('2 项活动 · 1 次工具调用')
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
