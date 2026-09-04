import type { TimelineEntry } from './api/types'
import { projectThinkingMarkup } from './thinking-markup'

const ACTIVITY_KINDS = new Set<TimelineEntry['kind']>([
  'commentary',
  'thinking',
  'progress',
  'tool',
  'compaction',
  'work_product',
  'agent',
  'plan',
])

export interface TurnPresentation {
  activity: TimelineEntry[]
  foreground: TimelineEntry[]
  toolCount: number
  mcpCount: number
  skillCount: number
  skillName: string | null
  requiresAttention: boolean
  errorCount: number
  terminalStatus: string | null
}

function callFamily(item: TimelineEntry) {
  const family = item.callView?.family
  return typeof family === 'string' ? family.toLowerCase() : ''
}

export function turnPresentation(
  response: TimelineEntry[],
  userText = '',
  streaming = false,
  clarificationAnswered = false,
): TurnPresentation {
  response = projectThinkingMarkup(response, streaming)
  // Earlier assistant segments followed by more tool work are progress,
  // while the answer stays on the reading surface. Never rewrite the journal.
  const lastTool = response.findLastIndex((item) => (
    item.kind === 'tool' || item.status === 'waiting_for_user'
  ))
  const activity: TimelineEntry[] = []
  const foreground: TimelineEntry[] = []
  response.forEach((item, index) => {
    // Plans are live controls, not historical conversation cards. The transcript retains them.
    if (item.kind === 'plan') return
    if (item.kind === 'assistant' && index < lastTool) activity.push({ ...item, kind: 'commentary' })
    else if (ACTIVITY_KINDS.has(item.kind)) activity.push(item)
    else foreground.push(item)
  })
  const tools = activity.filter((item) => item.kind === 'tool')
  const skillMatch = userText.trim().match(/^\/skill:([^\s]+)/i)
  return {
    activity,
    foreground,
    toolCount: tools.length,
    mcpCount: tools.filter((item) => callFamily(item) === 'mcp').length,
    skillCount: tools.filter((item) => callFamily(item) === 'skill').length,
    skillName: skillMatch?.[1] ?? null,
    // A historical failed attempt is audit information, not an open approval.
    // Runs routinely recover from tool errors before producing their answer.
    requiresAttention: tools.some((item) => item.pendingApproval),
    errorCount: tools.filter((item) => item.isError || item.status === 'denied' || item.status === 'error').length,
    terminalStatus: (() => {
      const status = foreground.findLast((item) => (
      ['failed', 'cancelled', 'interrupted', 'waiting_for_user'].includes(item.status ?? '')
      ))?.status ?? null
      return status === 'waiting_for_user' && clarificationAnswered ? 'clarification_answered' : status
    })(),
  }
}

export function activityTitle(presentation: TurnPresentation, active: boolean) {
  if (presentation.requiresAttention) return '处理过程需要确认'
  if (!active && presentation.terminalStatus === 'failed') return '处理未完成'
  if (!active && ['cancelled', 'interrupted'].includes(presentation.terminalStatus ?? '')) return '处理已停止'
  if (!active && presentation.terminalStatus === 'waiting_for_user') return '等待补充信息'
  if (!active && presentation.terminalStatus === 'clarification_answered') return '已收到补充信息'
  if (presentation.skillName) return `${active ? '正在运行' : '已运行'} Skill · ${presentation.skillName}`
  return active ? '正在处理' : '已完成处理'
}

export function activityMeta(presentation: TurnPresentation) {
  const parts = [`${presentation.activity.length} 项活动`]
  if (presentation.toolCount) parts.push(`${presentation.toolCount} 次工具调用`)
  if (presentation.mcpCount) parts.push(`MCP ${presentation.mcpCount}`)
  if (presentation.skillCount) parts.push(`Skill ${presentation.skillCount}`)
  if (presentation.errorCount) parts.push(`${presentation.errorCount} 次失败记录`)
  return parts.join(' · ')
}

export function activityDuration(seconds: number | undefined) {
  if (seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return null
  if (seconds < 1) return '不足 1s'
  const whole = Math.floor(seconds)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor(whole % 3600 / 60)
  const remainder = whole % 60
  return [hours && `${hours}h`, minutes && `${minutes}m`, remainder && `${remainder}s`]
    .filter(Boolean).join(' ')
}

export function toolActivityLabel(item: TimelineEntry) {
  const succeeded = !item.isError && ['completed', 'ok', 'success'].includes(item.status ?? '')
  const verb = succeeded ? item.callView?.completed_verb : item.callView?.active_verb
  const title = typeof verb === 'string' && verb ? verb : item.callView?.title ?? item.toolName ?? '工具'
  const detail = item.callView?.detail
  return `${String(title)}${typeof detail === 'string' && detail ? ` · ${detail}` : ''}`
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
