import type {
  ApprovalMode,
  ApiErrorBody,
  Bootstrap,
  EventEnvelope,
  EventType,
  FileSearchItem,
  ContextSourceItem,
  HookSummaryItem,
  McpPromptItem,
  QueueMode,
  RunStartedResponse,
  SessionSnapshot,
  SessionSummary,
} from './types'

const apiBaseUrl = process.env.NEXT_PUBLIC_LUMEN_API_BASE?.replace(/\/$/, '') ?? ''

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
  bootstrap: () => requestJson<Bootstrap>('/api/v1/bootstrap'),
  listSessions: async () =>
    (await requestJson<{ sessions: SessionSummary[] }>('/api/v1/sessions')).sessions,
  createSession: () =>
    requestJson<{ sessionId: string }>('/api/v1/sessions', { method: 'POST' }),
  session: (sessionId: string) =>
    requestJson<SessionSnapshot>(`/api/v1/sessions/${sessionId}`),
  startRun: (sessionId: string, input: string, clientRequestId: string) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/runs`, {
      method: 'POST',
      body: JSON.stringify({ input, clientRequestId }),
    }),
  retryRun: (sessionId: string, clientRequestId: string) =>
    requestJson<RunStartedResponse>(`/api/v1/sessions/${sessionId}/retry`, {
      method: 'POST',
      body: JSON.stringify({ clientRequestId }),
    }),
  cancelRun: (runId: string) =>
    requestJson<{ status: string }>(`/api/v1/runs/${runId}/cancel`, { method: 'POST' }),
  queueInput: (runId: string, text: string, mode: QueueMode) =>
    requestJson<{ status: string; message_id?: string }>(`/api/v1/runs/${runId}/input`, {
      method: 'POST',
      body: JSON.stringify({ text, mode }),
    }),
  decideApproval: (runId: string, callId: string, approved: boolean) =>
    requestJson<{ status: string }>(`/api/v1/runs/${runId}/approvals/${callId}`, {
      method: 'POST',
      body: JSON.stringify({ approved }),
    }),
  updateSettings: (settings: { model?: string; approvalMode?: ApprovalMode; confirmed?: boolean }) =>
    requestJson<Record<string, unknown>>('/api/v1/workspace/settings', {
      method: 'PATCH',
      body: JSON.stringify(settings),
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
  'clarification.requested',
  'plan.created',
  'plan.updated',
  'progress.reported',
  'tool.started',
  'tool.finished',
  'approval.pending',
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
