import type {
  AssistantState,
  AssistantContextRef,
  AssistantGoalDetail,
  AutomationAction,
  AssistantEdit,
  AssistantResource,
  AssistantFeedPage,
  AssistantResourcePage,
  AutomationOccurrence,
  ApprovalMode,
  ApprovalScope,
  AttachmentRef,
  AgentRecord,
  CheckpointRecord,
  CapabilityInventory,
  CollaborationMode,
  ConfigurationSnapshot,
  ApiErrorBody,
  Bootstrap,
  EventEnvelope,
  EventType,
  FileSearchItem,
  ContextSourceItem,
  HookSummaryItem,
  LiveEventEnvelope,
  LiveStartResponse,
  McpPromptItem,
  ModelConfigurationInput,
  ReasoningSelection,
  QueueMode,
  RunStartedResponse,
  SessionSnapshot,
  SessionSummary,
  TurnRestorePreview,
} from './types'

import { checkAuthentication } from './errors'

const apiBaseUrl = process.env.NEXT_PUBLIC_LUMEN_API_BASE?.replace(/\/$/, '') ?? ''

export function resourceFileUrl(id: string, revision: number) {
  return `${apiBaseUrl}/api/v1/assistant/resources/${encodeURIComponent(id)}/content?revision=${revision}`
}

export function liveMediaWebSocketUrl(path: string): string {
  const base = apiBaseUrl || window.location.origin
  const url = new URL(path, base)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    checkAuthentication(response)
    let message = `请求失败 (${response.status})`
    try {
      const body = (await response.json()) as ApiErrorBody
      message = body.error?.message || message
    } catch {
      // Keep the status-based fallback for non-JSON proxy failures.
    }
    throw new Error(message)
  }
  return (await response.json()) as T
}

export async function exchangeLaunchToken() {
  const url = new URL(window.location.href)
  const token = url.searchParams.get('token')
  if (!token) return
  const response = await fetch(`${apiBaseUrl}/auth/exchange?token=${encodeURIComponent(token)}`, {
    credentials: 'include',
  })
  if (!response.ok) {
    if (response.status !== 401) throw new Error(`无法连接 Lumen (${response.status})`)
    // A consumed launch link is harmless when this browser already has a valid cookie.
    const authenticated = await fetch(`${apiBaseUrl}/api/v1/bootstrap`, {credentials: 'include'})
    checkAuthentication(authenticated)
    if (!authenticated.ok) throw new Error('无法连接 Lumen，请稍后重试。')
  }
  url.searchParams.delete('token')
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
}

export const lumenApi = {
  preferences: () => requestJson<{ available: boolean; records: Array<{
    id: string; content: string; scope: string; source_kind: string; status: string; updated_at: string
  }> }>('/api/v1/assistant/preferences'),
  editPreference: (body: {
    action: 'remember' | 'edit' | 'forget' | 'confirm'; id?: string; content?: string
    scope?: 'user' | 'project'; expected_updated_at?: string
  }) => requestJson<{ status: string }>('/api/v1/assistant/preferences', {
    method: 'POST', body: JSON.stringify(body),
  }),
  assistant: () => requestJson<AssistantState>('/api/v1/assistant'),
  assistantGoal: (id: string) => requestJson<AssistantGoalDetail>(`/api/v1/assistant/goals/${encodeURIComponent(id)}`),
  controlGoal: (body: import('./types').GoalCommand) => requestJson<AssistantGoalDetail>('/api/v1/assistant/goals/control', {method: 'POST', body: JSON.stringify(body)}),
  assistantDiscussion: (context: AssistantContextRef) => requestJson<{session_id: string; context_refs: AssistantContextRef[]}>(
    '/api/v1/assistant/discussions', {method: 'POST', body: JSON.stringify({context})},
  ),
  assistantContext: (id: string) => requestJson<{session_id: string; context_refs: AssistantContextRef[]}>(`/api/v1/assistant/discussions/${encodeURIComponent(id)}`),
  publishAnswer: (body: {session_id: string; interaction_id: string; title: string; goal_id?: string; target_resource_id?: string; expected_revision?: number}) =>
    requestJson<{resource: AssistantResource}>('/api/v1/assistant/publications', {method: 'POST', body: JSON.stringify(body)}),
  assistantOccurrence: (id: string) => requestJson<AutomationOccurrence>(
    `/api/v1/assistant/occurrences/${encodeURIComponent(id)}`,
  ),
  assistantFeed: (category = 'all', before?: number) => requestJson<AssistantFeedPage>(
    `/api/v1/assistant/feed?${new URLSearchParams({ category, ...(before ? { before: String(before) } : {}) })}`,
  ),
  assistantResources: (query = '', category = 'all', before?: number, sessionId?: string) => requestJson<AssistantResourcePage>(
    `/api/v1/assistant/resources?${new URLSearchParams({ query, category, ...(before ? { before: String(before) } : {}), ...(sessionId ? {session_id: sessionId} : {}) })}`,
  ),
  readResourceFile: async (id: string, revision: number, signal?: AbortSignal) => {
    const response = await fetch(resourceFileUrl(id, revision), {credentials: 'include', signal})
    checkAuthentication(response)
    if (!response.ok) throw new Error('资料文件暂不可用，请返回资料空间后重试。')
    return response.blob()
  },
  importResourceFile: async (file: File, identity: string): Promise<AssistantResource> => {
    const response = await fetch(`${apiBaseUrl}/api/v1/assistant/resources/import?${new URLSearchParams({filename: file.name, identity})}`, {
      method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/octet-stream'}, body: file,
    })
    checkAuthentication(response)
    if (!response.ok) throw new Error(response.status === 413 ? '文件超过 20 MiB 上限。' : '文件导入失败，请检查文件名与内容后重试。')
    return response.json()
  },
  automation: (body: Pick<AutomationAction, 'action' | 'request_id'> & Partial<AutomationAction>) =>
    requestJson<{ id: string }>('/api/v1/assistant/automations', {
      method: 'POST', body: JSON.stringify(body),
    }),
  editAssistant: (body: Pick<AssistantEdit, 'action' | 'id'> & Partial<AssistantEdit>) =>
    requestJson<{ status: string }>('/api/v1/assistant/state', {
      method: 'POST', body: JSON.stringify(body),
    }),
  assistantResource: (id: string, historical = false, revision?: number) => requestJson<{resource: AssistantResource; content: string; historical: boolean; goal_ids?: string[]; collection_ids?: string[]}>(
    `/api/v1/assistant/resources/${encodeURIComponent(id)}?${new URLSearchParams({historical: String(historical), ...(revision ? {revision: String(revision)} : {})})}`,
  ),
  readAttachment: async (attachment: AttachmentRef, signal?: AbortSignal): Promise<Blob> => {
    const response = await fetch(`${apiBaseUrl}/api/v1/attachments/content`, {
      method: 'POST', credentials: 'include', signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(attachment),
    })
    checkAuthentication(response)
    if (!response.ok) throw new Error(`图片读取失败 (${response.status})`)
    return response.blob()
  },
  readDocument: async (path: string, signal?: AbortSignal): Promise<Blob> => {
    const response = await fetch(`${apiBaseUrl}/api/v1/files/content?path=${encodeURIComponent(path)}`, { credentials: 'include', signal })
    checkAuthentication(response)
    if (!response.ok) {
      const body = await response.json().catch(() => null) as ApiErrorBody | null
      throw new Error(body?.error?.message || `文档读取失败 (${response.status})`)
    }
    return response.blob()
  },
  bootstrap: () => requestJson<Bootstrap>('/api/v1/bootstrap'),
  capabilities: () => requestJson<CapabilityInventory>('/api/v1/capabilities'),
  configuration: () => requestJson<ConfigurationSnapshot>('/api/v1/configuration'),
  inspectReasoning: (definition: ModelConfigurationInput) => requestJson<{ reasoning: ReasoningSelection }>(
    '/api/v1/configuration/reasoning', { method: 'POST', body: JSON.stringify(definition) },
  ),
  upsertModelConfiguration: (name: string, input: ModelConfigurationInput) =>
    requestJson<ConfigurationSnapshot & { status: string }>(
      `/api/v1/configuration/models/${encodeURIComponent(name)}`,
      { method: 'PUT', body: JSON.stringify(input) },
    ),
  deleteModelConfiguration: (name: string, expectedRevision: string) =>
    requestJson<ConfigurationSnapshot & { status: string }>(
      `/api/v1/configuration/models/${encodeURIComponent(name)}`,
      { method: 'DELETE', body: JSON.stringify({ expectedRevision }) },
    ),
  setMcpServerEnabled: (name: string, expectedRevision: string, enabled: boolean) =>
    requestJson<ConfigurationSnapshot & { status: string }>(
      `/api/v1/configuration/mcp/${encodeURIComponent(name)}`,
      { method: 'PATCH', body: JSON.stringify({ expectedRevision, enabled }) },
    ),
  listSessions: async (includeArchived = false) =>
    (await requestJson<{ sessions: SessionSummary[] }>(
      `/api/v1/sessions${includeArchived ? '?include_archived=true' : ''}`,
    )).sessions.map((session) => ({ ...session, archived: session.archived ?? false })),
  createSession: () =>
    requestJson<{ sessionId: string }>('/api/v1/sessions', { method: 'POST' }),
  previewTurnRestore: (sessionId: string, beforeTurn: number) =>
    requestJson<TurnRestorePreview>(`/api/v1/sessions/${sessionId}/restore-files?before_turn=${beforeTurn}`),
  applyTurnRestore: (preview: TurnRestorePreview, clientRequestId: string) =>
    requestJson<{ status: string; failed: { resource: string; error: string } | null }>(
      `/api/v1/sessions/${preview.session_id}/restore-files`, {
        method: 'POST', body: JSON.stringify({ before_turn: preview.before_turn,
          expected_digest: preview.digest, client_request_id: clientRequestId, confirmed: true }),
      },
    ),
  renameSession: (sessionId: string, title: string) =>
    requestJson<{ status: string; title: string }>(`/api/v1/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  archiveSession: (sessionId: string) =>
    requestJson<{ status: string }>(`/api/v1/sessions/${sessionId}/archive`, {
      method: 'POST',
    }),
  restoreSession: (sessionId: string) =>
    requestJson<{ status: string }>(`/api/v1/sessions/${sessionId}/archive`, {
      method: 'DELETE',
    }),
  deleteSession: (sessionId: string) =>
    requestJson<{ status: string }>(`/api/v1/sessions/${sessionId}`, {
      method: 'DELETE',
    }),
  session: (sessionId: string) =>
    requestJson<SessionSnapshot>(`/api/v1/sessions/${sessionId}`),
  listAgents: async (sessionId: string) =>
    (await requestJson<{ items: AgentRecord[] }>(
      `/api/v1/sessions/${sessionId}/agents`,
    )).items,
  agentAction: (
    agentId: string,
    action: string,
    payload: Record<string, unknown> = {},
  ) => requestJson<Record<string, unknown>>(`/api/v1/agents/${agentId}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action, ...payload }),
  }),
  uploadAttachment: (file: File) =>
    requestJson<AttachmentRef>(
      `/api/v1/attachments?filename=${encodeURIComponent(file.name)}`,
      { method: 'POST', body: file, headers: { 'Content-Type': file.type } },
    ),
  startRun: (
    sessionId: string,
    input: string,
    clientRequestId: string,
    attachments: AttachmentRef[] = [],
    regenerateFromTurn?: number,
    contextRefs: AssistantContextRef[] = [],
    publishResult = false,
  ) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/runs`, {
      method: 'POST',
      body: JSON.stringify({ input, clientRequestId, attachments, regenerateFromTurn, contextRefs, publishResult }),
    }),
  retryRun: (sessionId: string, clientRequestId: string) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/retry`, {
      method: 'POST',
      body: JSON.stringify({ clientRequestId }),
    }),
  startLive: (
    sessionId: string,
    sdp: string | null,
    clientRequestId: string,
    route?: string,
  ) =>
    requestJson<LiveStartResponse>(`/api/v1/sessions/${sessionId}/live`, {
      method: 'POST',
      body: JSON.stringify({ sdp, clientRequestId, route }),
    }),
  interruptLive: (liveSessionId: string) =>
    requestJson<Record<string, unknown>>(`/api/v1/live/${liveSessionId}/interrupt`, {
      method: 'POST',
    }),
  endLive: (liveSessionId: string) =>
    requestJson<Record<string, unknown>>(`/api/v1/live/${liveSessionId}`, {
      method: 'DELETE',
    }),
  decideLiveApproval: (
    liveSessionId: string,
    callId: string,
    approved: boolean,
    scope: ApprovalScope = 'once',
  ) => requestJson<{ status: string }>(
    `/api/v1/live/${liveSessionId}/approvals/${callId}`,
    { method: 'POST', body: JSON.stringify({ approved, scope }) },
  ),
  cancelRun: (runId: string) =>
    requestJson<{ status: string }>(`/api/v1/runs/${runId}/cancel`, { method: 'POST' }),
  queueInput: (
    runId: string,
    text: string,
    mode: QueueMode,
    attachments: AttachmentRef[] = [],
  ) =>
    requestJson<{ status: string; message_id?: string }>(`/api/v1/runs/${runId}/input`, {
      method: 'POST',
      body: JSON.stringify({ text, mode, attachments }),
    }),
  decideApproval: (
    runId: string,
    callId: string,
    approved: boolean,
    scope: ApprovalScope = 'once',
  ) =>
    requestJson<{ status: string; resolved_call_ids?: string[] }>(`/api/v1/runs/${runId}/approvals/${callId}`, {
      method: 'POST',
      body: JSON.stringify({ approved, scope }),
    }),
  updateWorkspaceSettings: (settings: { model?: string }) =>
    requestJson<Record<string, unknown>>('/api/v1/workspace/settings', {
      method: 'PATCH',
      body: JSON.stringify(settings),
    }),
  updateSessionSettings: (
    sessionId: string,
    settings: {
      approvalMode?: ApprovalMode
      permissionPreset?: import('./types').PermissionPreset
      confirmFullAccess?: boolean
      collaborationMode?: CollaborationMode
      transcriptDensity?: 'normal' | 'verbose'
      reasoningEffort?: import('./types').ReasoningLevel
    },
  ) => requestJson<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/settings`, {
    method: 'PATCH',
    body: JSON.stringify(settings),
  }),
  reviewPlan: (
    sessionId: string,
    action: 'approve' | 'reject',
    revision: number,
    clientRequestId: string,
    feedback = '',
  ) => requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/plan-review`, {
    method: 'POST',
    body: JSON.stringify({ action, revision, clientRequestId, feedback }),
  }),
  searchFiles: async (query: string) =>
    (
      await requestJson<{ items: FileSearchItem[] }>(
        `/api/v1/files/search?q=${encodeURIComponent(query)}`,
      )
    ).items,
  context: (sessionId: string) =>
    requestJson<{ status: string; message: string; payload: Record<string, unknown> }>(
      `/api/v1/sessions/${sessionId}/context`,
    ),
  contextSources: async (sessionId: string) =>
    (
      await requestJson<{ items: ContextSourceItem[] }>(
        `/api/v1/sessions/${sessionId}/context/sources`,
      )
    ).items,
  instructions: () => requestJson<{
    status: string
    mode: string
    preset: string | null
    version: string
    characters: number
    runtime_context_characters: number
    active_model: string
    model_id: string
    sources: Array<{ origin: string; role: string; revision: string; characters: number }>
  }>('/api/v1/instructions'),
  mcpPrompts: async () =>
    (await requestJson<{ items: McpPromptItem[] }>('/api/v1/mcp/prompts')).items,
  hooks: async () =>
    (await requestJson<{ items: HookSummaryItem[] }>('/api/v1/hooks')).items,
  compactContext: (sessionId: string, focus = '') =>
    requestJson<{ status: string; message: string }>(
      `/api/v1/sessions/${sessionId}/controls`,
      { method: 'POST', body: JSON.stringify({ type: 'compact_context', arguments: focus }) },
    ),
  memoryControl: (sessionId: string, action: string, payload: Record<string, unknown> = {}) =>
    requestJson<{ status: string; message: string; payload: Record<string, unknown> }>(
      `/api/v1/sessions/${sessionId}/controls`,
      { method: 'POST', body: JSON.stringify({ type: 'memory', action, payload }) },
    ),
  invokeSkill: (
    sessionId: string,
    name: string,
    args: string,
    clientRequestId: string,
    regenerateFromTurn?: number,
  ) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/controls`, {
      method: 'POST',
      body: JSON.stringify({
        type: 'invoke_skill', name, arguments: args, clientRequestId, regenerateFromTurn,
      }),
    }),
  invokePrompt: (
    sessionId: string,
    reference: string,
    args: Record<string, string>,
    displayInput: string,
    clientRequestId: string,
    regenerateFromTurn?: number,
  ) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/controls`, {
      method: 'POST',
      body: JSON.stringify({
        type: 'invoke_prompt',
        name: reference,
        arguments: displayInput,
        payload: { arguments: args },
        clientRequestId,
        regenerateFromTurn,
      }),
    }),
  setContextSource: (
    sessionId: string,
    kind: 'skill' | 'resource',
    reference: string,
    active: boolean,
  ) =>
    requestJson<{ status: string }>(`/api/v1/sessions/${sessionId}/controls`, {
      method: 'POST',
      body: JSON.stringify({
        type: 'set_context_source',
        payload: { kind, reference, active },
      }),
    }),
  cancelClarification: (sessionId: string) =>
    requestJson<{ status: string }>(`/api/v1/sessions/${sessionId}/controls`, {
      method: 'POST',
      body: JSON.stringify({ type: 'cancel_clarification' }),
    }),
  dequeueInputs: (runId: string) =>
    requestJson<{
      status: string
      items: Array<{ id: string; text: string; model_prompt: string; mode: QueueMode }>
    }>(`/api/v1/runs/${runId}/input/dequeue`, { method: 'POST' }),
  listCheckpoints: async (sessionId: string) =>
    (await requestJson<{ items: CheckpointRecord[] }>(
      `/api/v1/sessions/${sessionId}/checkpoints`,
    )).items,
  forkSession: (sessionId: string, throughTurn: number, options?: { includeTurn: boolean; clientRequestId: string }) =>
    requestJson<{ sessionId: string }>(`/api/v1/sessions/${sessionId}/fork`, {
      method: 'POST',
      body: JSON.stringify({ throughTurn, ...options }),
    }),
  waiveVerification: (sessionId: string, scope: string[], reason: string) =>
    requestJson<Record<string, unknown>>(
      `/api/v1/sessions/${sessionId}/verification-waivers`,
      { method: 'POST', body: JSON.stringify({ scope, reason }) },
    ),
}

const eventTypes: EventType[] = [
  'run.started',
  'run.completed',
  'run.waiting_for_user',
  'run.failed',
  'run.cancelled',
  'assistant.delta',
  'assistant.retracted',
  'commentary.delta',
  'thinking.delta',
  'clarification.requested',
  'plan.created',
  'plan.updated',
  'work_product.changed',
  'agent.lifecycle',
  'plan.review_pending',
  'plan.review_resolved',
  'progress.reported',
  'tool.started',
  'tool.finished',
  'approval.pending',
  'approval.batch_pending',
  'approval.resolved',
  'usage.updated',
  'context.compaction.started',
  'context.compaction.completed',
  'context.compaction.failed',
  'input.queued',
  'input.delivered',
  'input.dequeued',
]

export function subscribeRun(
  runId: string,
  onEvent: (event: EventEnvelope) => void,
  onError: (message: string) => void,
) {
  const source = new EventSource(`${apiBaseUrl}/api/v1/runs/${runId}/events`, {
    withCredentials: true,
  })
  const listener = (message: MessageEvent<string>) => {
    try {
      const event = JSON.parse(message.data) as EventEnvelope
      onEvent(event)
      if (
        event.type === 'run.completed' ||
        event.type === 'run.waiting_for_user' ||
        event.type === 'run.failed' ||
        event.type === 'run.cancelled'
      ) {
        source.close()
      }
    } catch {
      onError('无法解析运行事件')
      source.close()
    }
  }
  for (const type of eventTypes) source.addEventListener(type, listener as EventListener)
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onError('运行事件连接已关闭')
  }
  return () => source.close()
}

const liveEventTypes = [
  'live.session.created',
  'live.session.connected',
  'live.session.reconnecting',
  'live.session.ended',
  'live.session.failed',
  'live.turn.started',
  'live.input.speech_started',
  'live.input.speech_stopped',
  'live.input.transcript.delta',
  'live.input.transcript.completed',
  'live.response.started',
  'live.response.audio_started',
  'live.response.transcript.delta',
  'live.response.completed',
  'live.response.interrupted',
  'live.tool.started',
  'live.tool.approval_pending',
  'live.tool.finished',
  'live.usage.updated',
  'live.reconciliation_required',
]

export function subscribeLive(
  liveSessionId: string,
  onEvent: (event: LiveEventEnvelope) => void,
  onError: (message: string) => void,
) {
  const source = new EventSource(`${apiBaseUrl}/api/v1/live/${liveSessionId}/events`, {
    withCredentials: true,
  })
  const listener = (message: MessageEvent<string>) => {
    try {
      const event = JSON.parse(message.data) as LiveEventEnvelope
      onEvent(event)
      if (event.type === 'live.session.ended' || event.type === 'live.session.failed') {
        source.close()
      }
    } catch {
      onError('无法解析实时语音事件')
      source.close()
    }
  }
  for (const type of liveEventTypes) source.addEventListener(type, listener as EventListener)
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onError('实时语音状态连接已关闭')
  }
  return () => source.close()
}
