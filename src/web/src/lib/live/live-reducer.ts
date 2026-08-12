import type { LiveEventEnvelope } from '@/lib/api/types'

export type LiveUiStatus =
  | 'idle'
  | 'requesting_permission'
  | 'connecting'
  | 'listening'
  | 'user_speaking'
  | 'processing'
  | 'approval_pending'
  | 'tool_running'
  | 'assistant_speaking'
  | 'interrupted'
  | 'reconnecting'
  | 'ended'
  | 'error'

export interface LiveApprovalState {
  callId: string
  name: string
  risk: string
  arguments: Record<string, unknown>
}

export interface LiveUiState {
  status: LiveUiStatus
  liveSessionId: string | null
  muted: boolean
  userTranscript: string
  assistantTranscript: string
  error: string | null
  approval: LiveApprovalState | null
}

export const initialLiveState: LiveUiState = {
  status: 'idle',
  liveSessionId: null,
  muted: false,
  userTranscript: '',
  assistantTranscript: '',
  error: null,
  approval: null,
}

export type LiveAction =
  | { type: 'status'; status: LiveUiStatus }
  | { type: 'connected'; liveSessionId: string }
  | { type: 'muted'; muted: boolean }
  | { type: 'event'; event: LiveEventEnvelope }
  | { type: 'error'; message: string }
  | { type: 'approval-resolved' }
  | { type: 'reset' }

export function liveReducer(state: LiveUiState, action: LiveAction): LiveUiState {
  if (action.type === 'reset') return initialLiveState
  if (action.type === 'status') return { ...state, status: action.status, error: null }
  if (action.type === 'connected') {
    return { ...state, liveSessionId: action.liveSessionId, status: 'listening', error: null }
  }
  if (action.type === 'muted') return { ...state, muted: action.muted }
  if (action.type === 'approval-resolved') {
    return { ...state, approval: null, status: 'processing' }
  }
  if (action.type === 'error') return { ...state, status: 'error', error: action.message }

  const { event } = action
  switch (event.type) {
    case 'live.session.connected':
      return { ...state, status: 'listening', error: null }
    case 'live.session.reconnecting':
      return { ...state, status: 'reconnecting' }
    case 'live.session.ended':
      return { ...state, status: 'ended', liveSessionId: null, approval: null }
    case 'live.session.failed':
    case 'live.reconciliation_required':
      return {
        ...state,
        status: 'error',
        error: String(event.data.message ?? '实时语音连接失败'),
      }
    case 'live.input.speech_started':
      return { ...state, status: 'user_speaking', userTranscript: '' }
    case 'live.input.speech_stopped':
      return { ...state, status: 'processing' }
    case 'live.input.transcript.delta':
      return { ...state, userTranscript: state.userTranscript + String(event.data.delta ?? '') }
    case 'live.input.transcript.completed':
      return { ...state, userTranscript: String(event.data.transcript ?? '') }
    case 'live.response.started':
      return { ...state, status: 'processing', assistantTranscript: '' }
    case 'live.response.audio_started':
      return { ...state, status: 'assistant_speaking' }
    case 'live.response.transcript.delta':
      return {
        ...state,
        assistantTranscript: state.assistantTranscript + String(event.data.delta ?? ''),
      }
    case 'live.response.completed':
      return { ...state, status: 'listening', approval: null }
    case 'live.response.approved':
      return { ...state, status: 'assistant_speaking', error: null }
    case 'live.completion.blocked': {
      const issues = Array.isArray(event.data.issues)
        ? event.data.issues.map(String)
        : ['存在未完成的验证或协调事项']
      return {
        ...state,
        status: 'listening',
        error: `暂不能确认完成：${issues.join('；')}`,
      }
    }
    case 'live.response.interrupted':
      return { ...state, status: 'interrupted' }
    case 'live.tool.started':
      return { ...state, status: 'tool_running' }
    case 'live.tool.approval_pending':
      return {
        ...state,
        status: 'approval_pending',
        approval: {
          callId: String(event.data.call_id ?? ''),
          name: String(event.data.name ?? 'tool'),
          risk: String(event.data.risk ?? 'unknown'),
          arguments: (event.data.arguments as Record<string, unknown> | undefined) ?? {},
        },
      }
    case 'live.tool.finished':
      return { ...state, status: 'processing', approval: null }
    default:
      return state
  }
}
