'use client'

import {
  Microphone,
  MicrophoneSlash,
  PhoneDisconnect,
  StopCircle,
  Waveform,
  ArrowsOut,
} from '@phosphor-icons/react'
import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { lumenApi } from '@/lib/api/client'
import type { LiveEventEnvelope } from '@/lib/api/types'
import { initialLiveState, liveReducer } from '@/lib/live/live-reducer'
import { RealtimeVoiceClient, silentAudioLevels } from '@/lib/live/realtime-client'
import { ChoiceMenu } from './choice-menu'
import { VoiceFocus } from './voice-focus'

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
  const [focused, setFocused] = useState(false)
  const levels = useRef({ ...silentAudioLevels })
  const recoveryTimer = useRef<number | null>(null)
  const visualizing = useRef(false)
  const mutedRef = useRef(false)
  const generation = useRef(0)
  const expandButton = useRef<HTMLButtonElement>(null)
  const wasFocused = useRef(false)
  const setVisualizing = useCallback((active: boolean) => {
    visualizing.current = active
    clientRef.current?.setVisualizationActive(active)
  }, [])
  useEffect(() => {
    if (wasFocused.current && !focused) expandButton.current?.focus()
    wasFocused.current = focused
  }, [focused])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      generation.current++
      if (recoveryTimer.current !== null) window.clearTimeout(recoveryTimer.current)
      void clientRef.current?.stop()
    }
  }, [])

  if (!enabled) return null

  const applyEvent = (event: LiveEventEnvelope) => {
    if (mountedRef.current) dispatch({ type: 'event', event })
  }

  const connectClient = async (sessionId: string, recovering = false) => {
    const client = new RealtimeVoiceClient()
    clientRef.current = client
    client.setVisualizationActive(visualizing.current)
    client.setMuted(mutedRef.current)
    const token = generation.current
    const current = () => mountedRef.current && clientRef.current === client && sessionRef.current === sessionId
    const handleLost = (message: string) => {
      if (!current() || recoveringRef.current) return
      if (recovering) {
        dispatch({ type: 'error', message })
        return
      }
      recoveringRef.current = true
      dispatch({ type: 'status', status: 'reconnecting' })
      recoveryTimer.current = window.setTimeout(() => {
        recoveryTimer.current = null
        void (async () => {
          await client.stop()
          if (!current()) return
          try {
            await connectClient(sessionId, true)
          } catch (error) {
            if (!mountedRef.current || token !== generation.current) return
            dispatch({
              type: 'error',
              message: error instanceof Error ? error.message : '实时语音重连失败',
            })
          } finally {
            if (token === generation.current) recoveringRef.current = false
          }
        })()
      }, 600)
    }
    const handleEvent = (event: LiveEventEnvelope) => {
      if (!current()) return
      applyEvent(event)
      if (event.type === 'live.session.ended') void end()
      if (event.type === 'live.session.reconnecting') {
        handleLost('Live 会话正在安全续接')
      }
    }
    const liveSessionId = await client.connect(
      sessionId,
      {
        onEvent: handleEvent,
        onConnectionLost: handleLost,
        onPlaybackEnded: () => { if (current()) dispatch({ type: 'status', status: 'listening' }) },
        onAudioLevels: (next) => { if (current()) levels.current = next },
      },
      deviceId || undefined,
    )
    if (!current()) { await client.stop(); return }
    dispatch({ type: 'connected', liveSessionId })
    const available = await client.devices()
    if (current()) setDevices(available)
  }

  const start = async () => {
    if (blocked) return
    const token = ++generation.current
    dispatch({ type: 'status', status: 'requesting_permission' })
    try {
      const sessionId = await ensureSession()
      if (!mountedRef.current || token !== generation.current) return
      sessionRef.current = sessionId
      dispatch({ type: 'status', status: 'connecting' })
      await connectClient(sessionId)
    } catch (error) {
      if (!mountedRef.current || token !== generation.current) return
      await clientRef.current?.stop()
      clientRef.current = null
      dispatch({
        type: 'error',
        message: error instanceof Error ? error.message : '无法启动实时语音',
      })
    }
  }

  const end = async () => {
    generation.current++
    setFocused(false)
    if (recoveryTimer.current !== null) window.clearTimeout(recoveryTimer.current)
    recoveryTimer.current = null
    const sessionId = sessionRef.current
    const client = clientRef.current
    clientRef.current = null
    sessionRef.current = null
    recoveringRef.current = false
    dispatch({ type: 'reset' })
    levels.current = { ...silentAudioLevels }
    mutedRef.current = false
    setPushToTalk(false)
    await client?.stop()
    if (sessionId) onEnded?.(sessionId)
  }

  const toggleMute = () => {
    const muted = !state.muted
    mutedRef.current = muted
    clientRef.current?.setMuted(muted)
    dispatch({ type: 'muted', muted })
  }

  const togglePushToTalk = () => {
    const next = !pushToTalk
    setPushToTalk(next)
    mutedRef.current = next
    clientRef.current?.setMuted(next)
    dispatch({ type: 'muted', muted: next })
  }

  const holdToTalk = (speaking: boolean) => {
    if (!pushToTalk) return
    mutedRef.current = !speaking
    clientRef.current?.setMuted(!speaking)
    dispatch({ type: 'muted', muted: !speaking })
  }

  const resolveApproval = async (
    approved: boolean,
    scope: 'once' | 'session' = 'once',
  ) => {
    if (!state.liveSessionId || !state.approval) return
    try { await lumenApi.decideLiveApproval(
      state.liveSessionId,
      state.approval.callId,
      approved,
      scope,
    )
    dispatch({ type: 'approval-resolved' })
    } catch (error) { dispatch({ type: 'error', message: error instanceof Error ? error.message : '审批操作失败，请重试' }) }
  }

  const active = state.liveSessionId !== null
    || ['requesting_permission', 'connecting', 'reconnecting'].includes(state.status)

  const controls = <>
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
          {!focused && <button ref={expandButton} type="button" className="voice-icon-button" aria-label="展开语音视图" onClick={() => setFocused(true)}><ArrowsOut size={18} /></button>}
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
              onClick={() => void clientRef.current?.interrupt().catch(error => dispatch({ type: 'error', message: error instanceof Error ? error.message : '打断失败' }))}
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
            onPointerCancel={() => holdToTalk(false)}
            onKeyDown={event => { if (pushToTalk && [' ', 'Enter'].includes(event.key)) { event.preventDefault(); holdToTalk(true) } }}
            onKeyUp={event => { if (pushToTalk && [' ', 'Enter'].includes(event.key)) { event.preventDefault(); holdToTalk(false) } }}
            onBlur={() => holdToTalk(false)}
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
              <ChoiceMenu
                className="is-device"
                value={deviceId}
                label="麦克风设备"
                menuWidth={280}
                options={[
                  { value: '', label: '默认麦克风', description: '跟随系统当前输入设备' },
                  ...devices.map((device) => ({
                    value: device.deviceId,
                    label: device.label || `麦克风 ${device.deviceId.slice(0, 6)}`,
                  })),
                ]}
                onChange={async (next) => {
                  await clientRef.current?.selectDevice(next)
                  setDeviceId(next)
                }}
              />
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
    </>
  return <div className={`live-voice ${active ? 'is-active' : ''}`}>
    {focused && active ? <VoiceFocus levels={levels} status={state.status} muted={state.muted}
      onClose={() => setFocused(false)} onVisibility={setVisualizing}>{controls}</VoiceFocus> : controls}
  </div>
}
