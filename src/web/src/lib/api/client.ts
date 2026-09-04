import type {
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
  QueueMode,
  RunStartedResponse,
  SessionSnapshot,
  SessionSummary,
} from './types'

const apiBaseUrl = process.env.NEXT_PUBLIC_LUMEN_API_BASE?.replace(/\/$/, '') ?? ''

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
  if (!response.ok) throw new Error('启动链接已失效，请重新运行 lumen web')
  url.searchParams.delete('token')
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
}

export const lumenApi = {
  readDocument: async (path: string, signal?: AbortSignal): Promise<Blob> => {
    const response = await fetch(`${apiBaseUrl}/api/v1/files/content?path=${encodeURIComponent(path)}`, { credentials: 'include', signal })
    if (!response.ok) {
      const body = await response.json().catch(() => null) as ApiErrorBody | null
      throw new Error(body?.error?.message || `文档读取失败 (${response.status})`)
    }
    return response.blob()
  },
  bootstrap: () => requestJson<Bootstrap>('/api/v1/bootstrap'),
  capabilities: () => requestJson<CapabilityInventory>('/api/v1/capabilities'),
  configuration: () => requestJson<ConfigurationSnapshot>('/api/v1/configuration'),
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
  listSessions: async (includeArchived = false) =>
    (await requestJson<{ sessions: SessionSummary[] }>(
      `/api/v1/sessions${includeArchived ? '?include_archived=true' : ''}`,
    )).sessions.map((session) => ({ ...session, archived: session.archived ?? false })),
  createSession: () =>
    requestJson<{ sessionId: string }>('/api/v1/sessions', { method: 'POST' }),
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
  ) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/runs`, {
      method: 'POST',
      body: JSON.stringify({ input, clientRequestId, attachments }),
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
    requestJson<{ status: string }>(`/api/v1/runs/${runId}/approvals/${callId}`, {
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
      collaborationMode?: CollaborationMode
      transcriptDensity?: 'normal' | 'verbose'
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
  invokeSkill: (sessionId: string, name: string, args: string, clientRequestId: string) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/controls`, {
      method: 'POST',
      body: JSON.stringify({ type: 'invoke_skill', name, arguments: args, clientRequestId }),
    }),
  invokePrompt: (
    sessionId: string,
    reference: string,
    args: Record<string, string>,
    displayInput: string,
    clientRequestId: string,
  ) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/controls`, {
      method: 'POST',
      body: JSON.stringify({
        type: 'invoke_prompt',
        name: reference,
        arguments: displayInput,
        payload: { arguments: args },
        clientRequestId,
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
