import { useEffect, useRef, useState } from 'react'
import type { MascotActivity, MascotGesture, MascotMotionMode } from './mascot-types'

export const IDLE_ACTION_DELAY_RANGE_MS = [8_000, 18_000] as const
export const BLINK_DELAY_RANGE_MS = [3_000, 7_000] as const
export const REDUCED_BLINK_DELAY_RANGE_MS = [7_000, 12_000] as const
export const ALERT_GESTURE_MS = 1_650
export const STRETCH_GESTURE_MS = 1_850
export const FIRST_IDLE_ACTION_DELAY_MS = 2_200

export function randomDelay(
  random: () => number,
  [minimum, maximum]: readonly [number, number],
) {
  const sample = Math.min(1, Math.max(0, random()))
  return Math.round(minimum + (maximum - minimum) * sample)
}

export function nextIdleActionDelay(random: () => number = Math.random) {
  return randomDelay(random, IDLE_ACTION_DELAY_RANGE_MS)
}

export function nextBlinkDelay(
  motionMode: MascotMotionMode,
  random: () => number = Math.random,
) {
  const range = motionMode === 'reduced'
    ? REDUCED_BLINK_DELAY_RANGE_MS
    : BLINK_DELAY_RANGE_MS
  return randomDelay(random, range)
}

export function nextIdleGesture(random: () => number = Math.random): MascotGesture {
  return random() < 0.72 ? 'alert' : 'stretch'
}

export function gestureForActivity(
  activity: MascotActivity,
  idleGesture: MascotGesture = 'idle',
): MascotGesture {
  if (activity === 'submitting') return 'submit'
  return activity === 'idle' ? idleGesture : 'idle'
}

interface MascotDirectorOptions {
  activity: MascotActivity
  motionMode: MascotMotionMode
  active: boolean
}

export function useMascotDirector({ activity, motionMode, active }: MascotDirectorOptions) {
  const [idleGesture, setIdleGesture] = useState<MascotGesture>('idle')
  const [blinking, setBlinking] = useState(false)
  const hasPlayedWelcomeAction = useRef(false)

  useEffect(() => {
    if (activity !== 'idle' || motionMode !== 'full' || !active) {
      setIdleGesture('idle')
      return
    }

    let actionTimer = 0
    let resetTimer = 0
    let cancelled = false

    const scheduleAction = () => {
      const delay = hasPlayedWelcomeAction.current
        ? nextIdleActionDelay()
        : FIRST_IDLE_ACTION_DELAY_MS
      actionTimer = window.setTimeout(() => {
        if (cancelled) return
        const gesture = hasPlayedWelcomeAction.current ? nextIdleGesture() : 'alert'
        hasPlayedWelcomeAction.current = true
        setIdleGesture(gesture)
        resetTimer = window.setTimeout(() => {
          if (cancelled) return
          setIdleGesture('idle')
          scheduleAction()
        }, gesture === 'stretch' ? STRETCH_GESTURE_MS : ALERT_GESTURE_MS)
      }, delay)
    }

    scheduleAction()
    return () => {
      cancelled = true
      window.clearTimeout(actionTimer)
      window.clearTimeout(resetTimer)
    }
  }, [activity, motionMode, active])

  useEffect(() => {
    if (activity === 'submitting' || motionMode === 'static' || !active) {
      setBlinking(false)
      return
    }

    let blinkTimer = 0
    let resetTimer = 0
    let cancelled = false

    const scheduleBlink = () => {
      blinkTimer = window.setTimeout(() => {
        if (cancelled) return
        setBlinking(true)
        resetTimer = window.setTimeout(() => {
          if (cancelled) return
          setBlinking(false)
          scheduleBlink()
        }, 120)
      }, nextBlinkDelay(motionMode))
    }

    scheduleBlink()
    return () => {
      cancelled = true
      window.clearTimeout(blinkTimer)
      window.clearTimeout(resetTimer)
    }
  }, [activity, motionMode, active])

  return {
    gesture: gestureForActivity(activity, idleGesture),
    focused: activity === 'focused',
    blinking: blinking && activity !== 'submitting' && idleGesture === 'idle',
  }
}
