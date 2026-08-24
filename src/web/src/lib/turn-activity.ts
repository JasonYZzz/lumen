import type { TimelineEntry } from './api/types'

const ACTIVITY_KINDS = new Set<TimelineEntry['kind']>([
  'commentary',
  'thinking',
  'progress',
  'tool',
  'compaction',
  'work_product',
  'agent',
])

export interface TurnPresentation {
  activity: TimelineEntry[]
  foreground: TimelineEntry[]
  toolCount: number
  mcpCount: number
  skillCount: number
  skillName: string | null
  requiresAttention: boolean
}

function callFamily(item: TimelineEntry) {
  const family = item.callView?.family
  return typeof family === 'string' ? family.toLowerCase() : ''
}

export function turnPresentation(
  response: TimelineEntry[],
  userText = '',
): TurnPresentation {
  const activity = response.filter((item) => ACTIVITY_KINDS.has(item.kind))
  const foreground = response.filter((item) => !ACTIVITY_KINDS.has(item.kind))
  const tools = activity.filter((item) => item.kind === 'tool')
  const skillMatch = userText.trim().match(/^\/skill:([^\s]+)/i)
  return {
    activity,
    foreground,
    toolCount: tools.length,
    mcpCount: tools.filter((item) => callFamily(item) === 'mcp').length,
    skillCount: tools.filter((item) => callFamily(item) === 'skill').length,
    skillName: skillMatch?.[1] ?? null,
    requiresAttention: tools.some((item) => (
      item.pendingApproval
      || item.isError
      || item.status === 'denied'
      || item.status === 'error'
    )),
  }
}

export function activityTitle(presentation: TurnPresentation, active: boolean) {
  if (presentation.requiresAttention) return '处理过程需要确认'
  if (presentation.skillName) return `${active ? '正在运行' : '已运行'} Skill · ${presentation.skillName}`
  return active ? '正在处理' : '已完成处理'
}

export function activityMeta(presentation: TurnPresentation) {
  const parts = [`${presentation.activity.length} 项活动`]
  if (presentation.toolCount) parts.push(`${presentation.toolCount} 次工具调用`)
  if (presentation.mcpCount) parts.push(`MCP ${presentation.mcpCount}`)
  if (presentation.skillCount) parts.push(`Skill ${presentation.skillCount}`)
  return parts.join(' · ')
}

export function toolSourceLabel(item: TimelineEntry) {
  const family = callFamily(item)
  if (family === 'mcp') return 'MCP'
  if (family === 'skill') return 'Skill'
  if (family === 'web') return 'Web'
  return null
}

export function timelineNoteLabel(kind: TimelineEntry['kind']) {
  if (kind === 'commentary') return '进展'
  if (kind === 'thinking') return '思考'
  if (kind === 'progress') return '进度'
  if (kind === 'compaction') return '上下文'
  if (kind === 'error') return '错误'
  if (kind === 'work_product') return '工作对象'
  if (kind === 'agent') return 'Agent'
  return ''
}

export function transcriptEventLabel(item: TimelineEntry) {
  if (item.kind === 'user') return '你'
  if (item.kind === 'assistant') return 'Lumen'
  if (item.kind === 'plan') return '计划'
  if (item.kind === 'tool') return toolSourceLabel(item) ?? '工具'
  if (item.kind === 'system') return '事件'
  return timelineNoteLabel(item.kind) || '事件'
}
