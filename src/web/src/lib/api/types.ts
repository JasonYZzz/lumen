import type { components } from './schema.generated'

export type ApprovalMode = NonNullable<
  components['schemas']['SessionSettingsBody']['approvalMode']
>
export type CollaborationMode = NonNullable<
  components['schemas']['SessionSettingsBody']['collaborationMode']
>
export type QueueMode = components['schemas']['QueueInputBody']['mode']
export type ApprovalScope = 'once' | 'session' | 'always'
export type ReasoningLevel = NonNullable<components['schemas']['SessionSettingsBody']['reasoningEffort']>

export interface ReasoningSelection {
  requested: ReasoningLevel | null
  effective: ReasoningLevel | null
  source: string
  mapping: string
  supported_levels: ReasoningLevel[]
  parameters: Record<string, unknown>
  capability_status?: 'supported' | 'unsupported' | 'unknown'
  capability_source?: string
  catalog_revision?: string | null
  provider?: string | null
  capability_documents?: string[]
  capability_reviewed_on?: string | null
  capability_note?: string
  provider_default_level?: ReasoningLevel | null
  level_map?: Partial<Record<ReasoningLevel, ReasoningLevel>>
}

export interface AttachmentRef {
  artifactRef: string
  kind: 'image'
  mediaType: string
  filename: string
  byteSize: number
}

export interface AgentRecord {
  id: string
  status: string
  task?: string
  role?: string
  path?: string
  task_generation?: number
  result_summary?: string | null
  workspace_mode?: string
  [key: string]: unknown
}

export interface CheckpointRecord {
  index: number
  created_at: string
  status: string
  prompt: string
  receipt_count: number
  mutation_count: number
}

export interface Bootstrap {
  reasoning?: ReasoningSelection
  agent: string
  workspace: string
  activeModel: string
  modelId: string
  inputModalities: Array<'text' | 'image'>
  availableModels: string[]
  approvalMode: ApprovalMode
  collaborationMode: CollaborationMode
  tools: Array<{ name: string; origin: string; risk: string; effect?: string }>
  mcp: Array<Record<string, unknown>>
  skills: Array<{ name: string; description: string }>
  warnings: string[]
  activeRunId: string | null
  liveEnabled: boolean
}

export interface ConfiguredModel {
  reasoningProfile?: string | null
  reasoningEffort?: ReasoningLevel | null
  reasoningLevels?: ReasoningLevel[] | null
  name: string
  id: string
  api: 'chat' | 'responses' | 'openai-completions' | 'openai-responses' | 'chat-completions' | null
  baseUrl: string | null
  apiKeyEnv: string | null
  settings: Record<string, unknown>
  context: Record<string, unknown>
  inputModalities: Array<'text' | 'image'>
  nativeWebSearch?: {
    mode?: 'auto' | 'enabled' | 'disabled'
    search_context_size?: 'low' | 'medium' | 'high'
  }
  nativeWebSearchEnabled?: boolean
  isDefault: boolean
  source: { scope: string; path: string } | null
  authKind: 'environment' | 'inline' | 'none'
  authAvailable: boolean
}

export interface ConfiguredMcpServer {
  name: string
  enabled: boolean
  source: { scope: string; path: string } | null
}

export interface ConfigurationSnapshot {
  revision: string
  targetPath: string
  editable: boolean
  editReason: string | null
  exclusive: boolean
  sources: Array<{ scope: string; path: string }>
  warnings: string[]
  defaultModel: string
  activeModel?: string
  models: ConfiguredModel[]
  mcpServers?: ConfiguredMcpServer[]
  restartRequired?: boolean
}

export interface CapabilityInventory {
  tools: Array<Record<string, unknown>>
  skills: Array<Record<string, unknown>>
  mcp_servers: Array<Record<string, unknown>>
  agent_profiles: Array<Record<string, unknown>>
}

export interface ModelConfigurationInput {
  reasoningProfile?: string | null
  reasoningEffort?: ReasoningLevel | null
  reasoningLevels?: ReasoningLevel[] | null
  expectedRevision: string
  id: string
  api?: ConfiguredModel['api']
  baseUrl?: string | null
  apiKeyEnv?: string | null
  settings?: Record<string, unknown>
  context?: Record<string, unknown>
  inputModalities?: Array<'text' | 'image'>
  nativeWebSearch?: ConfiguredModel['nativeWebSearch']
  setDefault?: boolean
}

export interface LiveSessionState {
  ref: { id: string; session_id: string; provider: string }
  connection: string
  activity: string
  model: string
  voice: string
  turn_count: number
  usage: Record<string, unknown>
  last_user_transcript?: string | null
  last_assistant_transcript?: string | null
  pending_call_ids: string[]
  error?: string | null
  created_at: string
  updated_at: string
}

export interface LiveStartResponse {
  liveSessionId: string
  sessionId: string
  answerSdp: string | null
  media: {
    kind: 'direct_webrtc' | 'host_websocket' | 'managed_rtc'
    answer_sdp?: string
    media_path?: string
    input_sample_rate?: number
    output_sample_rate?: number
  }
  state: LiveSessionState
}

export interface LiveEventEnvelope {
  version: 1
  sequence: number
  sessionId: string
  liveSessionId: string
  type: string
  createdAt: string
  data: Record<string, unknown>
}

export interface SessionSummary {
  sessionId: string
  createdAt: string
  modelId: string
  title: string
  archived: boolean
  titlePending?: boolean
}

export interface PlanStep {
  id: string
  title: string
  depends_on: string[]
  acceptance_criteria: Array<{ id: string; description: string }>
  status: 'pending' | 'in_progress' | 'completed' | 'blocked' | 'skipped'
  note?: string | null
}

export interface PlanState {
  goal: string
  revision: number
  state_version: number
  lifecycle: string
  approved_revision: number | null
  steps: PlanStep[]
}

export type TimelineKind =
  | 'user'
  | 'assistant'
  | 'plan'
  | 'commentary'
  | 'thinking'
  | 'progress'
  | 'tool'
  | 'system'
  | 'error'
  | 'compaction'
  | 'work_product'
  | 'agent'

export interface TimelineEntry {
  id: string
  kind: TimelineKind
  text: string
  elapsedSeconds?: number
  turnIndex?: number
  interactionId?: string
  attachments?: AttachmentRef[]
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
  callView?: Record<string, unknown>
  resultView?: Record<string, unknown>
  plan?: PlanState
}

export interface SessionSnapshot {
  reasoning?: ReasoningSelection
  sessionId: string
  modelId: string
  createdAt: string
  plan: PlanState
  timeline: Array<Record<string, unknown>>
  lastUserInput: string | null
  activeRunId: string | null
  approvalMode: ApprovalMode
  collaborationMode: CollaborationMode
  planReviewStatus: string
  transcriptDensity: 'normal' | 'verbose'
  pendingClarification: {
    id: string
    question: string
    choices: string[]
    related_plan_step?: string | null
    created_at: string
  } | null
  workProducts: Array<Record<string, unknown>>
  pendingEffects: Array<Record<string, unknown>>
  recoverableEffects: Array<Record<string, unknown>>
  agents: AgentRecord[]
  agentUsage: Record<string, unknown>
  liveSessions: LiveSessionState[]
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
  | 'thinking.delta'
  | 'clarification.requested'
  | 'plan.created'
  | 'plan.updated'
  | 'work_product.changed'
  | 'agent.lifecycle'
  | 'plan.review_pending'
  | 'plan.review_resolved'
  | 'progress.reported'
  | 'tool.started'
  | 'tool.finished'
  | 'approval.pending'
  | 'approval.batch_pending'
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
