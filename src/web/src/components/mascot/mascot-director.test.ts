import { describe, expect, it } from 'vitest'
import {
  FIRST_IDLE_ACTION_DELAY_MS,
  gestureForActivity,
  nextBlinkDelay,
  nextIdleActionDelay,
  nextIdleGesture,
  randomDelay,
} from './mascot-director'

describe('mascot director', () => {
  it('shows one early welcome action so motion is discoverable', () => {
    expect(FIRST_IDLE_ACTION_DELAY_MS).toBe(4_800)
  })

  it('keeps automatic actions inside the calm 14–26 second window', () => {
    expect(nextIdleActionDelay(() => 0)).toBe(14_000)
    expect(nextIdleActionDelay(() => 1)).toBe(26_000)
  })

  it('uses only complete authored poses for automatic actions', () => {
    expect(nextIdleGesture(() => 0)).toBe('alert')
    expect(nextIdleGesture(() => 1)).toBe('stretch')
  })

  it('slows blinking down in reduced motion mode', () => {
    expect(nextBlinkDelay('full', () => 0)).toBe(4_000)
    expect(nextBlinkDelay('full', () => 1)).toBe(8_000)
    expect(nextBlinkDelay('reduced', () => 0)).toBe(9_000)
    expect(nextBlinkDelay('reduced', () => 1)).toBe(14_000)
  })

  it('keeps every state on the same cohesive character rig', () => {
    expect(gestureForActivity('idle', 'alert')).toBe('alert')
    expect(gestureForActivity('focused', 'stretch')).toBe('idle')
    expect(gestureForActivity('submitting', 'alert')).toBe('submit')
  })

  it('clamps injected random samples before calculating a delay', () => {
    expect(randomDelay(() => -2, [100, 200])).toBe(100)
    expect(randomDelay(() => 3, [100, 200])).toBe(200)
  })
})
