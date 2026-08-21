import type {
  AgentRecord,
  EventEnvelope,
  PlanState,
  SessionSnapshot,
  TimelineEntry,
} from '@/lib/api/types'

const CONTROL_TOOL_NAMES = new Set([
  'set_plan',
  'update_step',
  'link_evidence',
  'report_progress',
  'request_clarification',
])

export interface RunState {
  timeline: TimelineEntry[]
  plan: PlanState | null
  planReviewStatus: string | null
  planReviewRevision: number | null
  runId: string | null
  status: 'idle' | 'running' | 'waiting_for_user' | 'completed' | 'failed' | 'cancelled'
  usage: Record<string, unknown> | null
  queuedInputs: Array<{ id: string; text: string; mode: string }>
  transcriptDensity: 'normal' | 'verbose'
  workProducts: Array<Record<string, unknown>>
  pendingEffects: Array<Record<string, unknown>>
  recoverableEffects: Array<Record<string, unknown>>
  agents: AgentRecord[]
  agentUsage: Record<string, unknown>
}

export const initialRunState: RunState = {
  timeline: [],
  plan: null,
  planReviewStatus: null,
  planReviewRevision: null,
  runId: null,
  status: 'idle',
  usage: null,
  queuedInputs: [],
  transcriptDensity: 'normal',
  workProducts: [],
  pendingEffects: [],
  recoverableEffects: [],
  agents: [],
  agentUsage: {},
}

export type RunAction =
  | { type: 'event'; event: EventEnvelope }
  | { type: 'snapshot'; snapshot: SessionSnapshot }
  | { type: 'run-registered'; runId: string }
  | { type: 'clear-visible' }
  | { type: 'reset' }
  | { type: 'local-message'; message: string }
  | { type: 'local-error'; message: string }
  | { type: 'agents-refreshed'; agents: AgentRecord[] }
  | { type: 'transcript-density'; density: 'normal' | 'verbose' }
  | {
      type: 'work-state-refreshed'
      workProducts: Array<Record<string, unknown>>
      pendingEffects: Array<Record<string, unknown>>
      recoverableEffects: Array<Record<string, unknown>>
    }

function string(value: unknown, fallback = '') {
  return typeof value === 'string' ? value : fallback
}

function boolean(value: unknown) {
  return value === true
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function replaceTool(
  timeline: TimelineEntry[],
  callId: string,
  update: (item: TimelineEntry) => TimelineEntry,
) {
  return timeline.map((item) => (item.callId === callId ? update(item) : item))
}

function appendText(
  timeline: TimelineEntry[],
  kind: 'assistant' | 'commentary' | 'thinking',
  text: string,
) {
  const last = timeline.at(-1)
  if (last?.kind === kind) {
    return [...timeline.slice(0, -1), { ...last, text: `${last.text}${text}` }]
  }
  return [...timeline, { id: `${kind}-${timeline.length + 1}`, kind, text }]
}

function upsertPlan(timeline: TimelineEntry[], plan: PlanState, sequence: number) {
  const latestUserIndex = timeline.findLastIndex((item) => item.kind === 'user')
  const planIndex = timeline.findLastIndex(
    (item, index) => item.kind === 'plan' && index > latestUserIndex,
  )
  if (planIndex >= 0) {
    const next = [...timeline]
    next[planIndex] = { ...next[planIndex], plan }
    return next
  }
  return [
    ...timeline,
    { id: `plan-${sequence}`, kind: 'plan' as const, text: '', plan },
  ]
}

function snapshotTimeline(items: Array<Record<string, unknown>>): TimelineEntry[] {
  return items.map((item, index) => ({
    id: string(item.id, `restored-${index}`),
    kind: string(item.kind, 'system') as TimelineEntry['kind'],
    text: string(item.text),
    callId: string(item.call_id ?? item.callId) || undefined,
    toolName: string(item.tool_name ?? item.toolName) || undefined,
    args: object(item.args),
    result: item.result == null ? null : string(item.result),
    preview: item.preview == null ? null : string(item.preview),
    status: item.status == null ? null : string(item.status),
    pendingApproval: boolean(item.pending_approval ?? item.pendingApproval),
    isError: boolean(item.is_error ?? item.isError),
    callView: object(item.call_view ?? item.callView),
    resultView: object(item.result_view ?? item.resultView),
    plan: item.plan && typeof item.plan === 'object'
      ? item.plan as unknown as PlanState
      : undefined,
  }))
}

export function runReducer(state: RunState, action: RunAction): RunState {
  if (action.type === 'reset') return initialRunState
  if (action.type === 'agents-refreshed') return { ...state, agents: action.agents }
  if (action.type === 'transcript-density') {
    return { ...state, transcriptDensity: action.density }
  }
  if (action.type === 'work-state-refreshed') {
    return {
      ...state,
      workProducts: action.workProducts,
      pendingEffects: action.pendingEffects,
      recoverableEffects: action.recoverableEffects,
    }
  }
  if (action.type === 'clear-visible') return { ...state, timeline: [] }
  if (action.type === 'local-message') {
    return {
      ...state,
      timeline: [
        ...state.timeline,
        { id: `system-${state.timeline.length + 1}`, kind: 'system', text: action.message },
      ],
    }
  }
  if (action.type === 'local-error') {
    return {
      ...state,
      status: 'failed',
      timeline: [
        ...state.timeline,
        { id: `error-${state.timeline.length + 1}`, kind: 'error', text: action.message },
      ],
    }
  }
  if (action.type === 'run-registered') {
    return { ...state, runId: action.runId, status: 'running' }
  }
  if (action.type === 'snapshot') {
    return {
      ...initialRunState,
      timeline: snapshotTimeline(action.snapshot.timeline),
      plan: action.snapshot.plan,
      planReviewStatus: action.snapshot.planReviewStatus,
      planReviewRevision: action.snapshot.plan.revision || null,
      runId: action.snapshot.activeRunId,
      status: action.snapshot.activeRunId ? 'running' : 'idle',
      transcriptDensity: action.snapshot.transcriptDensity,
      workProducts: action.snapshot.workProducts,
      pendingEffects: action.snapshot.pendingEffects,
      recoverableEffects: action.snapshot.recoverableEffects,
      agents: action.snapshot.agents,
      agentUsage: action.snapshot.agentUsage,
    }
  }

  const { event } = action
  const data = event.data
  if (event.type === 'run.started') {
    return {
      ...state,
      runId: event.runId,
      status: 'running',
      timeline: [
        ...state.timeline,
        { id: `user-${event.sequence}`, kind: 'user', text: string(data.prompt) },
      ],
    }
  }
  if (event.type === 'assistant.delta') {
    return { ...state, timeline: appendText(state.timeline, 'assistant', string(data.text)) }
  }
  if (event.type === 'assistant.retracted') {
    const characters = typeof data.characters === 'number' ? data.characters : 0
    const index = state.timeline.findLastIndex((item) => item.kind === 'assistant')
    if (index < 0) return state
    const item = state.timeline[index]
    const remaining = characters ? item.text.slice(0, -characters) : item.text
    const timeline = [...state.timeline]
    if (remaining) timeline[index] = { ...item, text: remaining }
    else timeline.splice(index, 1)
    return { ...state, timeline }
  }
  if (event.type === 'commentary.delta') {
    return { ...state, timeline: appendText(state.timeline, 'commentary', string(data.text)) }
  }
  if (event.type === 'thinking.delta') {
    return { ...state, timeline: appendText(state.timeline, 'thinking', string(data.text)) }
  }
  if (event.type === 'clarification.requested') {
    const choices = Array.isArray(data.choices)
      ? data.choices.map((item) => `- ${String(item)}`).join('\n')
      : ''
    const text = `${string(data.question)}${choices ? `\n${choices}` : ''}`
    return {
      ...state,
      timeline: [
        ...state.timeline,
        { id: `clarification-${event.sequence}`, kind: 'system', text, status: 'waiting_for_user' },
      ],
    }
  }
  if (event.type === 'plan.created' || event.type === 'plan.updated') {
    const plan = data.plan as unknown as PlanState
    return { ...state, plan, timeline: upsertPlan(state.timeline, plan, event.sequence) }
  }
  if (event.type === 'plan.review_pending') {
    const plan = data.plan as unknown as PlanState
    const revision = typeof data.revision === 'number' ? data.revision : plan.revision
    return {
      ...state,
      plan,
      planReviewStatus: 'review_pending',
      planReviewRevision: revision,
      timeline: upsertPlan(state.timeline, plan, event.sequence),
    }
  }
  if (event.type === 'plan.review_resolved') {
    return {
      ...state,
      planReviewStatus: boolean(data.approved) ? 'approved' : 'rejected',
      planReviewRevision: typeof data.revision === 'number' ? data.revision : state.planReviewRevision,
    }
  }
  if (event.type === 'progress.reported') {
    const next = string(data.next_action)
    const text = `${string(data.summary)}${next ? `\n${next}` : ''}`
    return {
      ...state,
      timeline: [...state.timeline, { id: `progress-${event.sequence}`, kind: 'progress', text }],
    }
  }
  if (event.type === 'work_product.changed') {
    const resource = string(data.resource)
    const phase = string(data.phase, 'updated')
    const detail = string(data.summary, string(data.status))
    const text = `${phase}${resource ? ` — ${resource}` : ''}: ${detail}`
    return {
      ...state,
      timeline: [
        ...state.timeline,
        {
          id: `work-product-${event.sequence}`,
          kind: 'work_product',
          text,
          status: string(data.status) || null,
        },
      ],
    }
  }
  if (event.type === 'tool.started') {
    if (string(data.origin) === 'control' || CONTROL_TOOL_NAMES.has(string(data.name))) {
      return state
    }
    return {
      ...state,
      timeline: [
        ...state.timeline,
        {
          id: `tool-${event.sequence}`,
          kind: 'tool',
          text: '',
          callId: string(data.call_id),
          toolName: string(data.name),
          args: object(data.args),
          status: 'running',
          callView: object(data.call_view),
        },
      ],
    }
  }
  if (event.type === 'approval.pending') {
    const callId = string(data.call_id)
    const presentation = object(data.presentation)
    const entry = {
      presentation: {
        title: string(presentation.title),
        preview: string(presentation.preview),
        full_text: string(presentation.full_text),
      },
      pendingApproval: true,
      status: 'pending',
    }
    const exists = state.timeline.some((item) => item.callId === callId)
    return {
      ...state,
      timeline: exists
        ? replaceTool(state.timeline, callId, (item) => ({ ...item, ...entry }))
        : [
            ...state.timeline,
            {
              id: `tool-${event.sequence}`,
              kind: 'tool',
              text: '',
              callId,
              toolName: string(data.name),
              args: object(data.args),
              ...entry,
            },
          ],
    }
  }
  if (event.type === 'approval.batch_pending') {
    const requests = Array.isArray(data.requests) ? data.requests : []
    const presentations = Array.isArray(data.presentations) ? data.presentations : []
    let timeline = state.timeline
    requests.forEach((rawRequest, index) => {
      const request = object(rawRequest)
      const presentation = object(presentations[index])
      const callId = string(request.call_id)
      const entry: TimelineEntry = {
        id: `tool-${event.sequence}-${index}`,
        kind: 'tool',
        text: '',
        callId,
        toolName: string(request.name),
        args: object(request.args),
        pendingApproval: true,
        status: 'pending',
        presentation: {
          title: string(presentation.title),
          preview: string(presentation.preview),
          full_text: string(presentation.full_text),
        },
      }
      timeline = timeline.some((item) => item.callId === callId)
        ? replaceTool(timeline, callId, (item) => ({ ...item, ...entry, id: item.id }))
        : [...timeline, entry]
    })
    return { ...state, timeline }
  }
  if (event.type === 'agent.lifecycle') {
    const agentId = string(data.agent_id)
    const existing = state.agents.find((agent) => agent.id === agentId)
    const updated: AgentRecord = {
      ...(existing ?? { id: agentId }),
      id: agentId,
      path: string(data.path, existing?.path),
      status: string(data.status, existing?.status ?? 'unknown'),
      result_summary: string(data.summary, existing?.result_summary ?? ''),
    }
    const agents = existing
      ? state.agents.map((agent) => (agent.id === agentId ? updated : agent))
      : [...state.agents, updated]
    const text = `${string(data.path, agentId)} · ${string(data.phase)} · ${updated.status}`
      + (updated.result_summary ? ` — ${updated.result_summary}` : '')
    return {
      ...state,
      agents,
      timeline: [
        ...state.timeline,
        { id: `agent-${event.sequence}`, kind: 'agent', text, status: updated.status },
      ],
    }
  }
  if (event.type === 'approval.resolved') {
    const callId = string(data.call_id)
    return {
      ...state,
      timeline: replaceTool(state.timeline, callId, (item) => ({
        ...item,
        pendingApproval: false,
        approved: boolean(data.approved),
        status: boolean(data.approved) ? 'approved' : 'denied',
      })),
    }
  }
  if (event.type === 'tool.finished') {
    const callId = string(data.call_id)
    return {
      ...state,
      timeline: replaceTool(state.timeline, callId, (item) => ({
        ...item,
        result: string(data.result),
        preview: string(data.preview),
        isError: boolean(data.is_error),
        pendingApproval: false,
        status: 'completed',
        resultView: object(data.result_view),
      })),
    }
  }
  if (event.type === 'usage.updated') {
    return { ...state, usage: object(data.usage) }
  }
  if (event.type.startsWith('context.compaction.')) {
    const text = event.type.endsWith('started')
      ? 'Compacting context…'
      : event.type.endsWith('failed')
        ? `Context compaction failed: ${string(data.message)}`
        : `Context compacted: ${String(data.active_message_count ?? 0)} active messages.`
    return {
      ...state,
      timeline: [
        ...state.timeline,
        { id: `compaction-${event.sequence}`, kind: 'compaction', text },
      ],
    }
  }
  if (event.type === 'input.queued') {
    return {
      ...state,
      queuedInputs: [
        ...state.queuedInputs,
        { id: string(data.message_id), text: string(data.text), mode: string(data.mode) },
      ],
    }
  }
  if (event.type === 'input.delivered' || event.type === 'input.dequeued') {
    return {
      ...state,
      queuedInputs: state.queuedInputs.filter((item) => item.id !== string(data.message_id)),
    }
  }
  if (event.type === 'run.completed') {
    return { ...state, status: 'completed', runId: null }
  }
  if (event.type === 'run.waiting_for_user') {
    return { ...state, status: 'waiting_for_user', runId: null }
  }
  if (event.type === 'run.cancelled') {
    return {
      ...state,
      status: 'cancelled',
      runId: null,
      timeline: [
        ...state.timeline,
        { id: `cancelled-${event.sequence}`, kind: 'system', text: string(data.message, 'Run cancelled') },
      ],
    }
  }
  if (event.type === 'run.failed') {
    return {
      ...state,
      status: 'failed',
      runId: null,
      timeline: [
        ...state.timeline,
        { id: `error-${event.sequence}`, kind: 'error', text: string(data.message, 'Run failed') },
      ],
    }
  }
  return state
}
