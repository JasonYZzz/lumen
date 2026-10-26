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
  // A turn owns one final reading answer. Providers may stream status narration as
  // assistant text before a tool, between native server-side tools, or before more
  // thinking. Keep those earlier segments in the process disclosure without
  // rewriting the append-only journal.
  const lastTool = response.findLastIndex((item) => (
    item.kind === 'tool' || item.status === 'waiting_for_user'
  ))
  const lastAssistant = response.findLastIndex((item) => item.kind === 'assistant')
  const lastActivity = response.findLastIndex((item) => ACTIVITY_KINDS.has(item.kind))
  const explicitCommentary = new Set(response
    .filter((item) => item.kind === 'commentary')
    .map((item) => item.text.replace(/\s+/g, ' ').trim())
    .filter(Boolean))
  const activity: TimelineEntry[] = []
  const foreground: TimelineEntry[] = []
  response.forEach((item, index) => {
    // Plans are live controls, not historical conversation cards. The transcript retains them.
    if (item.kind === 'plan') return
    const assistantIsProcess = item.kind === 'assistant'
      && index < Math.max(lastTool, lastAssistant, lastActivity)
    if (assistantIsProcess) {
      const normalized = item.text.replace(/\s+/g, ' ').trim()
      // The native loop can first emit candidate text, then retract and re-emit the
      // exact same text as CommentaryDelta. Show that status once in the UI.
      if (!normalized || !explicitCommentary.has(normalized)) {
        activity.push({ ...item, kind: 'commentary' })
      }
    }
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
    errorCount: tools.filter((item) => item.isError || ['denied', 'error', 'failed'].includes(item.status ?? '')).length,
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
  if (active) {
    const running = presentation.activity.filter(isRunningTool)
    if (running.length > 1) return `${running.length} 项操作进行中`
    if (running.length === 1) {
      const family = callFamily(running[0])
      if (family === 'web') return running[0].toolName === 'web_fetch' ? '正在阅读网页' : '正在搜索资料'
      if (family === 'read' || family === 'list') return '正在阅读文件'
      if (family === 'search') return '正在搜索文件'
      if (family === 'command') return '正在运行命令'
      if (family === 'edit') return '正在修改文件'
      return String(running[0].callView?.active_verb ?? '正在使用工具')
    }
    if (presentation.foreground.some((item) => item.kind === 'assistant')) return '正在生成回答'
  }
  return active ? '正在思考' : '已完成处理'
}

export function isRunningTool(item: TimelineEntry) {
  return item.kind === 'tool' && !item.pendingApproval && !item.isError
    && ['running', 'approved'].includes(item.status ?? '')
}

export interface ActivityGroup {
  id: string
  entries: TimelineEntry[]
  label: string | null
}

function completedToolsLabel(entries: TimelineEntry[]) {
  const counts = new Map<string, { verb: string; unit: string; count: number }>()
  for (const item of entries) {
    const webRead = item.toolName === 'web_fetch' && (!item.origin || item.origin.startsWith('builtin'))
    const verb = webRead ? '已阅读网页' : String(item.callView?.completed_verb ?? '已使用工具')
    const plural = item.callView?.plural
    const unit = webRead ? '个网页' : typeof plural === 'string' && plural !== 'calls' ? plural : '次'
    const key = `${verb}:${unit}`
    const previous = counts.get(key)
    if (previous) previous.count += 1
    else counts.set(key, { verb, unit, count: 1 })
  }
  return Array.from(counts.values(), ({ verb, count, unit }) => `${verb} ${count} ${unit}`).join(' · ')
}

/** A reading projection only: all original events remain available in the transcript. */
export function readableActivity(activity: TimelineEntry[]) {
  const groups: ActivityGroup[] = []
  const diagnostics: TimelineEntry[] = []
  let previousKey: string | null = null
  let previousText = ''
  for (const original of activity) {
    if (['compaction', 'work_product'].includes(original.kind)) {
      diagnostics.push(original)
      previousKey = null
      continue
    }
    if (original.kind === 'thinking') {
      // Reasoning belongs beside the actions it informed, not at the end of a diagnostics dump.
      if (original.text.trim()) groups.push({ id: original.id, entries: [original], label: null })
      previousKey = null
      previousText = ''
      continue
    }
    if (original.kind === 'agent' && !['failed', 'blocked', 'import_pending'].includes(original.status ?? '')) {
      diagnostics.push(original)
      previousKey = null
      continue
    }
    const text = original.text.split('\n').filter((line) => !/^正在准备工具调用:\s/.test(line.trim())).join('\n').trim()
    if (text !== original.text.trim()) diagnostics.push(original)
    if (original.kind !== 'tool' && !text) continue
    const item = text === original.text ? original : { ...original, text }
    if (['commentary', 'progress'].includes(item.kind)) {
      const normalized = text.replace(/\s+/g, ' ')
      if (normalized === previousText) continue
      previousText = normalized
    } else previousText = ''
    // This is a disclosure of completed receipts, independent of execution concurrency or risk.
    const key = item.kind === 'tool'
      && !item.pendingApproval && !item.isError && ['completed', 'ok', 'success'].includes(item.status ?? '')
      ? 'completed-tools' : null
    const previous = groups.at(-1)
    if (key && key === previousKey && previous) {
      previous.entries.push(item)
    } else groups.push({ id: item.id, entries: [item], label: null })
    previousKey = key
  }
  for (const group of groups) {
    if (group.entries.length > 1) group.label = completedToolsLabel(group.entries)
  }
  return { groups, diagnostics }
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
  const failed = item.isError || ['error', 'failed', 'denied'].includes(item.status ?? '')
  const webRead = item.toolName === 'web_fetch' && (!item.origin || item.origin.startsWith('builtin'))
  const verb = failed ? '操作未完成' : item.pendingApproval ? '等待确认'
    : item.status === 'cancelled' ? '操作已取消' : item.status === 'interrupted' ? '操作已中断'
    : webRead ? succeeded ? '已阅读网页' : '正在阅读网页'
    : succeeded ? item.callView?.completed_verb : item.callView?.active_verb
  const title = typeof verb === 'string' && verb ? verb : item.callView?.title ?? item.toolName ?? '工具'
  let detail = item.callView?.detail
  if (webRead && typeof item.args?.url === 'string') {
    try { detail = new URL(item.args.url).hostname } catch { /* Preserve declared presentation for invalid URLs. */ }
  }
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
