'use client'

import {
  Microphone,
  MicrophoneSlash,
  PhoneDisconnect,
  StopCircle,
  Waveform,
} from '@phosphor-icons/react'
import { useEffect, useReducer, useRef, useState } from 'react'
import { lumenApi } from '@/lib/api/client'
import type { LiveEventEnvelope } from '@/lib/api/types'
import { initialLiveState, liveReducer } from '@/lib/live/live-reducer'
import { RealtimeVoiceClient } from '@/lib/live/realtime-client'

export interface LiveVoiceControlsProps {
  enabled: boolean
  blocked: boolean
  ensureSession: () => Promise<string>
  onEnded?: (sessionId: string) => void
}

const statusLabels: Record<string, string> = {
  requesting_permission: '请求麦克风权限',
  connecting: '正在连接 Live',
  reconnecting: '正在恢复连接',
  listening: '正在聆听',
  user_speaking: '正在听你说话',
  processing: '正在思考',
  approval_pending: '等待工具审批',
  tool_running: '正在执行工具',
  assistant_speaking: 'Lumen 正在说话',
  interrupted: '已打断',
  error: '连接异常',
}

export function LiveVoiceControls({
  enabled,
  blocked,
  ensureSession,
  onEnded,
}: LiveVoiceControlsProps) {
  const [state, dispatch] = useReducer(liveReducer, initialLiveState)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [deviceId, setDeviceId] = useState('')
  const [pushToTalk, setPushToTalk] = useState(false)
  const clientRef = useRef<RealtimeVoiceClient | null>(null)
  const sessionRef = useRef<string | null>(null)
  const mountedRef = useRef(true)
  const recoveringRef = useRef(false)

  useEffect(() => () => {
    mountedRef.current = false
    void clientRef.current?.stop()
  }, [])

  if (!enabled) return null

  const applyEvent = (event: LiveEventEnvelope) => {
    if (mountedRef.current) dispatch({ type: 'event', event })
  }

  const connectClient = async (sessionId: string, recovering = false) => {
    const client = new RealtimeVoiceClient()
    clientRef.current = client
    const handleLost = (message: string) => {
      if (!mountedRef.current || recoveringRef.current) return
      if (recovering) {
        dispatch({ type: 'error', message })
        return
      }
      recoveringRef.current = true
      dispatch({ type: 'status', status: 'reconnecting' })
      window.setTimeout(() => {
        void (async () => {
          await client.stop()
          try {
            await connectClient(sessionId, true)
          } catch (error) {
            dispatch({
              type: 'error',
              message: error instanceof Error ? error.message : '实时语音重连失败',
            })
          } finally {
            recoveringRef.current = false
          }
        })()
      }, 600)
    }
    const handleEvent = (event: LiveEventEnvelope) => {
      applyEvent(event)
      if (event.type === 'live.session.reconnecting') {
        handleLost('Live 会话正在安全续接')
      }
    }
    const liveSessionId = await client.connect(
      sessionId,
      {
        onEvent: handleEvent,
        onConnectionLost: handleLost,
        onPlaybackEnded: () => dispatch({ type: 'status', status: 'listening' }),
      },
      deviceId || undefined,
    )
    dispatch({ type: 'connected', liveSessionId })
    setDevices(await client.devices())
  }

  const start = async () => {
    if (blocked) return
    dispatch({ type: 'status', status: 'requesting_permission' })
    try {
      const sessionId = await ensureSession()
      sessionRef.current = sessionId
      dispatch({ type: 'status', status: 'connecting' })
      await connectClient(sessionId)
    } catch (error) {
      await clientRef.current?.stop()
      clientRef.current = null
      dispatch({
        type: 'error',
        message: error instanceof Error ? error.message : '无法启动实时语音',
      })
    }
  }

  const end = async () => {
    const sessionId = sessionRef.current
    await clientRef.current?.stop()
    clientRef.current = null
    sessionRef.current = null
    recoveringRef.current = false
    dispatch({ type: 'reset' })
    if (sessionId) onEnded?.(sessionId)
  }

  const toggleMute = () => {
    const muted = !state.muted
    clientRef.current?.setMuted(muted)
    dispatch({ type: 'muted', muted })
  }

  const togglePushToTalk = () => {
    const next = !pushToTalk
    setPushToTalk(next)
    clientRef.current?.setMuted(next)
    dispatch({ type: 'muted', muted: next })
  }

  const holdToTalk = (speaking: boolean) => {
    if (!pushToTalk) return
    clientRef.current?.setMuted(!speaking)
    dispatch({ type: 'muted', muted: !speaking })
  }

  const resolveApproval = async (
    approved: boolean,
    scope: 'once' | 'session' = 'once',
  ) => {
    if (!state.liveSessionId || !state.approval) return
    await lumenApi.decideLiveApproval(
      state.liveSessionId,
      state.approval.callId,
      approved,
      scope,
    )
    dispatch({ type: 'approval-resolved' })
  }

  const active = state.liveSessionId !== null
    || ['requesting_permission', 'connecting', 'reconnecting'].includes(state.status)

  return (
    <div className={`live-voice ${active ? 'is-active' : ''}`}>
      {!active ? (
        <button
          className="voice-start-button"
          type="button"
          disabled={blocked}
          onClick={() => void start()}
          aria-label="开始实时语音"
          title={blocked ? '当前任务运行结束后可开始语音' : '开始实时语音'}
        >
          <Microphone size={18} weight="fill" />
        </button>
      ) : (
        <>
          <button
            className="voice-icon-button"
            type="button"
            onClick={toggleMute}
            aria-label={state.muted ? '取消静音' : '麦克风静音'}
          >
            {state.muted ? <MicrophoneSlash size={17} /> : <Microphone size={17} />}
          </button>
          {(state.status === 'assistant_speaking' || state.status === 'processing') && (
            <button
              className="voice-icon-button"
              type="button"
              onClick={() => void clientRef.current?.interrupt()}
              aria-label="打断回答"
            >
              <StopCircle size={18} />
            </button>
          )}
          <button
            className={`voice-icon-button ${pushToTalk ? 'is-selected' : ''}`}
            type="button"
            onClick={togglePushToTalk}
            onPointerDown={() => holdToTalk(true)}
            onPointerUp={() => holdToTalk(false)}
            onPointerLeave={() => holdToTalk(false)}
            aria-label="按住说话模式"
            title="按住说话"
          >
            <Waveform size={18} />
          </button>
          <button
            className="voice-end-button"
            type="button"
            onClick={() => void end()}
            aria-label="结束实时语音"
          >
            <PhoneDisconnect size={17} />
          </button>
        </>
      )}

      {active && (
        <div className="live-voice-popover" role="status" aria-live="polite">
          <div className="live-voice-heading">
            <span className={`live-wave is-${state.status}`} aria-hidden="true">
              <i /><i /><i /><i />
            </span>
            <strong>{statusLabels[state.status] ?? 'Live 已连接'}</strong>
            {devices.length > 1 && (
              <select
                value={deviceId}
                aria-label="麦克风设备"
                onChange={(event) => {
                  const next = event.target.value
                  setDeviceId(next)
                  void clientRef.current?.selectDevice(next)
                }}
              >
                <option value="">默认麦克风</option>
                {devices.map((device) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {device.label || `麦克风 ${device.deviceId.slice(0, 6)}`}
                  </option>
                ))}
              </select>
            )}
          </div>
          {(state.userTranscript || state.assistantTranscript) && (
            <div className="live-captions">
              {state.userTranscript && <p><span>你</span>{state.userTranscript}</p>}
              {state.assistantTranscript && <p><span>Lumen</span>{state.assistantTranscript}</p>}
            </div>
          )}
          {state.approval && (
            <div className="live-approval" role="alert">
              <strong>{state.approval.name}</strong>
              <span>{state.approval.risk}</span>
              <pre>{JSON.stringify(state.approval.arguments, null, 2)}</pre>
              <div>
                <button type="button" onClick={() => void resolveApproval(false)}>拒绝</button>
                <button type="button" onClick={() => void resolveApproval(true)}>允许一次</button>
                <button type="button" onClick={() => void resolveApproval(true, 'session')}>
                  本会话允许
                </button>
              </div>
            </div>
          )}
          {state.error && <p className="live-voice-error">{state.error}</p>}
        </div>
      )}
    </div>
  )
}
