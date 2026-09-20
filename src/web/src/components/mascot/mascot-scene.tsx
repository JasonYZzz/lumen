'use client'

import dynamic from 'next/dynamic'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useMascotDirector } from './mascot-director'
import { useMascotMotionPreference } from './mascot-motion-preference'
import type { MascotSceneProps } from './mascot-types'
import { useMascotVisibility } from './mascot-visibility'
import { RasterMascotRenderer } from './raster-mascot-renderer'
import { VideoMascotRenderer } from './video-mascot-renderer'
import styles from './mascot.module.css'
import { VisualBoundary } from '../visual-boundary'

const RiveMascotRenderer = dynamic(
  () => import('./rive-mascot-renderer').then((module) => module.RiveMascotRenderer),
  { ssr: false },
)

const RIVE_ENABLED = process.env.NEXT_PUBLIC_MASCOT_RENDERER === 'rive'
// No character asset is approved yet. Keep the loading seam dormant until visual acceptance.
const MODEL_APPROVED = false
const MODEL_URL = process.env.NEXT_PUBLIC_MASCOT_MODEL_URL
const ThreeMascotRenderer = dynamic(() => import('./three-mascot-renderer').then(module => module.ThreeMascotRenderer), {
  ssr: false, loading: () => <img src="/mascot/fox-poster.png" alt="" width={168} height={168} />,
})

export function MascotScene({ activity, motionMode, className }: MascotSceneProps) {
  return process.env.NEXT_PUBLIC_MASCOT_RENDERER === 'layered' || RIVE_ENABLED
    ? <LegacyMascotScene activity={activity} motionMode={motionMode} className={className} />
    : <AnimatedMascotScene activity={activity} motionMode={motionMode} className={className} />
}

function AnimatedMascotScene({ activity, motionMode, className }: MascotSceneProps) {
  const stageRef = useRef<HTMLDivElement>(null)
  const visible = useMascotVisibility(stageRef)
  const preference = useMascotMotionPreference(motionMode)
  const [interaction, setInteraction] = useState(0)
  const [resolved, setResolved] = useState(false)
  const [failed, setFailed] = useState(false)
  const unavailable = useCallback(() => setFailed(true), [])
  useEffect(() => setResolved(true), [])
  const useModel = MODEL_APPROVED && process.env.NEXT_PUBLIC_MASCOT_RENDERER === 'three' && MODEL_URL && resolved
    && preference.mode === 'full' && !failed
  return <div ref={stageRef} className={`${styles.stage} ${className ?? ''}`} data-active={visible}>
    <button type="button" className={styles.petButton} aria-label="和九尾狐打招呼"
      onClick={() => setInteraction(value => value + 1)}>
      {useModel ? visible && <VisualBoundary onFailure={unavailable} fallback={<img src="/mascot/fox-poster.png" alt="" width={168} height={168} />}>
        <ThreeMascotRenderer url={MODEL_URL!} activity={activity} active={visible}
          interaction={interaction} onUnavailable={unavailable} /></VisualBoundary>
        : <VideoMascotRenderer activity={activity} motionMode={preference.mode} active={visible} interaction={interaction} />}
    </button>
  </div>
}

/** Explicit rollback only; these older renderers never run alongside video playback. */
function LegacyMascotScene({ activity, motionMode, className }: MascotSceneProps) {
  const stageRef = useRef<HTMLDivElement>(null)
  const visible = useMascotVisibility(stageRef)
  const preference = useMascotMotionPreference(motionMode)
  const [riveStatus, setRiveStatus] = useState<'idle' | 'loading' | 'ready' | 'failed'>('idle')
  const active = visible && preference.mode !== 'static'
  const director = useMascotDirector({ activity, motionMode: preference.mode, active })
  const renderedGesture = preference.mode === 'static' ? 'idle' : director.gesture
  const renderedFocus = preference.mode !== 'static' && director.focused
  const shouldUseRive = RIVE_ENABLED && preference.mode === 'full' && riveStatus !== 'failed'
  const handleRiveReady = useCallback(() => setRiveStatus('ready'), [])
  const handleRiveUnavailable = useCallback(() => setRiveStatus('failed'), [])

  useEffect(() => {
    if (!shouldUseRive && riveStatus !== 'failed') setRiveStatus('idle')
    else if (shouldUseRive && riveStatus === 'idle') setRiveStatus('loading')
  }, [riveStatus, shouldUseRive])

  return (
    <div
      ref={stageRef}
      className={`${styles.stage} ${className ?? ''}`}
      data-active={active}
      data-renderer={riveStatus === 'ready' ? 'rive' : 'layered'}
    >
      <div aria-hidden="true">
        <div className={`${styles.rendererLayer} ${riveStatus === 'ready' ? styles.rendererHidden : ''}`}>
          <RasterMascotRenderer
            gesture={renderedGesture}
            focused={renderedFocus}
            blinking={preference.mode !== 'static' && director.blinking}
            motionMode={preference.mode}
            active={active}
          />
        </div>
        {shouldUseRive && (
          <div className={`${styles.rendererLayer} ${styles.riveLayer} ${riveStatus === 'ready' ? styles.rendererReady : ''}`}>
            <RiveMascotRenderer
              activity={activity}
              motionMode={preference.mode}
              active={active}
              onReady={handleRiveReady}
              onUnavailable={handleRiveUnavailable}
            />
          </div>
        )}
      </div>
    </div>
  )
}
