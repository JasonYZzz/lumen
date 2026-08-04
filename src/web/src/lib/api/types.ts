import type { components } from './schema.generated'

export type ApprovalMode = NonNullable<
  components['schemas']['WorkspaceSettingsBody']['approvalMode']
>
export type QueueMode = components['schemas']['QueueInputBody']['mode']

export interface Bootstrap {
  agent: string
  workspace: string
  activeModel: string
  modelId: string
  availableModels: string[]
  approvalMode: ApprovalMode
  tools: Array<{ name: string; origin: string; risk: string }>
  mcp: Array<Record<string, unknown>>
  skills: Array<{ name: string; description: string }>
  warnings: string[]
  activeRunId: string | null
}

export interface SessionSummary {
  sessionId: string
  createdAt: string
  modelId: string
  title: string
}

export interface PlanStep {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed' | 'blocked'
  note?: string | null
}

export interface PlanState {
  revision: number
  steps: PlanStep[]
}

export type TimelineKind =
  | 'user'
  | 'assistant'
  | 'plan'
  | 'commentary'
  | 'progress'
  | 'tool'
  | 'system'
  | 'error'
  | 'compaction'

export interface TimelineEntry {
  id: string
  kind: TimelineKind
  text: string
  callId?: string
  toolName?: string
  args?: Record<string, unknown>
  result?: string | null
  preview?: string | null
  status?: string | null
  pendingApproval?: boolean
  approved?: boolean
  isError?: boolean
  presentation?: { title: string; preview: string; full_text: string }
  plan?: PlanState
}

export interface SessionSnapshot {
  sessionId: string
  modelId: string
  createdAt: string
  plan: PlanState
  timeline: Array<Record<string, unknown>>
  lastUserInput: string | null
  activeRunId: string | null
  pendingClarification: {
    id: string
    question: string
    choices: string[]
    related_plan_step?: string | null
    created_at: string
  } | null
}

export type EventType =
  | 'run.started'
  | 'run.completed'
  | 'run.waiting_for_user'
  | 'run.failed'
  | 'run.cancelled'
  | 'assistant.delta'
  | 'assistant.retracted'
  | 'commentary.delta'
  | 'clarification.requested'
  | 'plan.created'
  | 'plan.updated'
  | 'progress.reported'
  | 'tool.started'
  | 'tool.finished'
  | 'approval.pending'
  | 'approval.resolved'
  | 'usage.updated'
  | 'context.compaction.started'
  | 'context.compaction.completed'
  | 'context.compaction.failed'
  | 'input.queued'
  | 'input.delivered'
  | 'input.dequeued'

export interface EventEnvelope {
  version: 1
  sequence: number
  sessionId: string
  runId: string
  type: EventType
  createdAt: string
  data: Record<string, unknown>
}

export interface RunStartedResponse {
  runId: string
  sessionId: string
  status: string
}

export interface FileSearchItem {
  path: string
  name: string
  isDirectory: boolean
}

export interface ContextSourceItem {
  kind: 'skill' | 'resource'
  reference: string
  revision: string
  status: string
}

export interface McpPromptItem {
  reference: string
  server: string
  name: string
  description: string
  arguments: string[]
}

export interface HookSummaryItem {
  event: string
  matcher: string
  runner: string
  deny_count: number
  last_triggered: string | null
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown }
}
