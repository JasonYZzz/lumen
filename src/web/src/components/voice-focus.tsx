'use client'

import dynamic from 'next/dynamic'
import { X } from '@phosphor-icons/react'
import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import type { AudioLevels } from '@/lib/live/realtime-client'
import type { LiveUiStatus } from '@/lib/live/live-reducer'
import { useModalFocus } from './use-modal-focus'
import { VisualBoundary } from './visual-boundary'
import { useSceneVisibility } from './use-scene-visibility'

const VoiceScene = dynamic(() => import('./voice-scene').then(module => module.VoiceScene), {
  ssr: false, loading: () => <div className="voice-static-shape" aria-hidden="true" />,
})

export function VoiceFocus({ children, levels, status, muted, onClose, onVisibility }: {
  children: ReactNode; levels: RefObject<AudioLevels>; status: LiveUiStatus; muted: boolean
  onClose: () => void; onVisibility: (active: boolean) => void
}) {
  const modal = useModalFocus(onClose)
  const visual = useRef<HTMLDivElement>(null)
  const visible = useSceneVisibility(visual)
  const [motionAllowed, setMotionAllowed] = useState(false)
  const [failed, setFailed] = useState(false)
  const unavailable = useCallback(() => setFailed(true), [])
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const sync = () => setMotionAllowed(!media.matches)
    sync()
    media.addEventListener('change', sync)
    return () => media.removeEventListener('change', sync)
  }, [])
  const running = motionAllowed && visible && !failed && !['approval_pending', 'error', 'reconnecting'].includes(status)
  useEffect(() => {
    onVisibility(running)
    return () => onVisibility(false)
  }, [running, onVisibility])
  return createPortal(<div className="voice-focus-backdrop">
    <section ref={modal} className="voice-focus" role="dialog" aria-modal="true" aria-label="实时语音专注视图" tabIndex={-1}>
      <header><strong>与 Lumen 对话</strong><button type="button" aria-label="收起语音视图" onClick={onClose}><X size={20} /></button></header>
      <div ref={visual} className="voice-focus-visual">
        {motionAllowed && !failed ? <VisualBoundary onFailure={unavailable} fallback={<div className="voice-static-shape" aria-hidden="true" />}>
          <VoiceScene levels={levels} status={status} muted={muted} active={visible} onUnavailable={unavailable} />
        </VisualBoundary>
          : <div className="voice-static-shape" aria-hidden="true" />}
      </div>
      <div className="voice-focus-controls">{children}</div>
      <p className="voice-focus-hint">收起视图会继续语音通话；结束通话请使用结束按钮。</p>
    </section>
  </div>, document.body)
}
