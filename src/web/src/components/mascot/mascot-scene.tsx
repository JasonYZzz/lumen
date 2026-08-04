'use client'

import dynamic from 'next/dynamic'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useMascotDirector } from './mascot-director'
import { useMascotMotionPreference } from './mascot-motion-preference'
import type { MascotSceneProps } from './mascot-types'
import { useMascotVisibility } from './mascot-visibility'
import { RasterMascotRenderer } from './raster-mascot-renderer'
import styles from './mascot.module.css'

const RiveMascotRenderer = dynamic(
  () => import('./rive-mascot-renderer').then((module) => module.RiveMascotRenderer),
  { ssr: false },
)

const RIVE_ENABLED = process.env.NEXT_PUBLIC_MASCOT_RENDERER === 'rive'

export function MascotScene({ activity, motionMode, className }: MascotSceneProps) {
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
