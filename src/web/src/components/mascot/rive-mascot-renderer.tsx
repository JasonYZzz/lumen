'use client'

import { useEffect } from 'react'
import { useRive, useStateMachineInput } from '@rive-app/react-canvas'
import type { MascotActivity, MascotMotionMode } from './mascot-types'
import styles from './mascot.module.css'

const STATE_MACHINE = 'Mascot'

export function riveActivityValue(activity: MascotActivity) {
  if (activity === 'focused') return 1
  if (activity === 'submitting') return 2
  return 0
}

export function riveMotionValue(motionMode: MascotMotionMode) {
  if (motionMode === 'reduced') return 1
  if (motionMode === 'static') return 2
  return 0
}

interface RiveMascotRendererProps {
  activity: MascotActivity
  motionMode: MascotMotionMode
  active: boolean
  onReady: () => void
  onUnavailable: () => void
}

export function RiveMascotRenderer({
  activity,
  motionMode,
  active,
  onReady,
  onUnavailable,
}: RiveMascotRendererProps) {
  const { rive, RiveComponent } = useRive({
    src: '/mascot/lumen-fox.riv',
    stateMachines: STATE_MACHINE,
    autoplay: active,
    shouldDisableRiveListeners: true,
    automaticallyHandleEvents: false,
    onRiveReady: (instance) => {
      if (!instance.stateMachineNames.includes(STATE_MACHINE)) {
        onUnavailable()
        return
      }
      instance.resizeDrawingSurfaceToCanvas()
      onReady()
    },
    onLoadError: onUnavailable,
  }, {
    shouldUseIntersectionObserver: false,
    shouldResizeCanvasToContainer: true,
  })

  const activityInput = useStateMachineInput(rive, STATE_MACHINE, 'activity', 0)
  const motionInput = useStateMachineInput(rive, STATE_MACHINE, 'motionLevel', 0)
  const submitInput = useStateMachineInput(rive, STATE_MACHINE, 'submit')

  useEffect(() => {
    if (activityInput) activityInput.value = riveActivityValue(activity)
  }, [activity, activityInput])

  useEffect(() => {
    if (motionInput) motionInput.value = riveMotionValue(motionMode)
  }, [motionMode, motionInput])

  useEffect(() => {
    if (activity === 'submitting') submitInput?.fire()
  }, [activity, submitInput])

  useEffect(() => {
    if (!rive) return
    if (active) rive.play(STATE_MACHINE)
    else rive.pause(STATE_MACHINE)
  }, [active, rive])

  return <RiveComponent className={styles.riveCanvas} aria-hidden="true" />
}
