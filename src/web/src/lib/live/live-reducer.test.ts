import { describe, expect, it } from 'vitest'
import { initialLiveState, liveReducer } from './live-reducer'

function event(type: string, data: Record<string, unknown> = {}) {
  return {
    version: 1 as const,
    sequence: 1,
    sessionId: 'session-1',
    liveSessionId: 'live-1',
    type,
    createdAt: '2026-01-01T00:00:00Z',
    data,
  }
}

describe('liveReducer', () => {
  it('tracks speech, captions, and assistant playback', () => {
    let state = liveReducer(initialLiveState, {
      type: 'connected',
      liveSessionId: 'live-1',
    })
    state = liveReducer(state, { type: 'event', event: event('live.input.speech_started') })
    state = liveReducer(state, {
      type: 'event',
      event: event('live.input.transcript.completed', { transcript: '你好' }),
    })
    state = liveReducer(state, {
      type: 'event',
      event: event('live.response.audio_started'),
    })
    state = liveReducer(state, {
      type: 'event',
      event: event('live.response.transcript.delta', { delta: '你好，' }),
    })
    state = liveReducer(state, {
      type: 'event',
      event: event('live.response.transcript.delta', { delta: '我是 Lumen。' }),
    })

    expect(state.status).toBe('assistant_speaking')
    expect(state.userTranscript).toBe('你好')
    expect(state.assistantTranscript).toBe('你好，我是 Lumen。')
  })

  it('surfaces approval without losing the live session', () => {
    const connected = liveReducer(initialLiveState, {
      type: 'connected',
      liveSessionId: 'live-1',
    })
    const pending = liveReducer(connected, {
      type: 'event',
      event: event('live.tool.approval_pending', {
        call_id: 'call-1',
        name: 'write_file',
        risk: 'write',
        arguments: { path: 'report.md' },
      }),
    })

    expect(pending.status).toBe('approval_pending')
    expect(pending.liveSessionId).toBe('live-1')
    expect(pending.approval).toEqual({
      callId: 'call-1',
      name: 'write_file',
      risk: 'write',
      arguments: { path: 'report.md' },
    })
  })

  it('projects a proactive rollover as reconnecting', () => {
    const connected = liveReducer(initialLiveState, {
      type: 'connected',
      liveSessionId: 'live-1',
    })
    const reconnecting = liveReducer(connected, {
      type: 'event',
      event: event('live.session.reconnecting', { reason: 'proactive_rollover' }),
    })

    expect(reconnecting.status).toBe('reconnecting')
    expect(reconnecting.liveSessionId).toBe('live-1')
  })

  it('surfaces a host completion block without ending the voice session', () => {
    const connected = liveReducer(initialLiveState, {
      type: 'connected',
      liveSessionId: 'live-1',
    })
    const blocked = liveReducer(connected, {
      type: 'event',
      event: event('live.completion.blocked', {
        issues: ['mutation verification pending', 'agent result not delivered'],
      }),
    })

    expect(blocked.status).toBe('listening')
    expect(blocked.liveSessionId).toBe('live-1')
    expect(blocked.error).toBe(
      '暂不能确认完成：mutation verification pending；agent result not delivered',
    )
  })
})
