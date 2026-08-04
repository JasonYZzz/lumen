import { describe, expect, it } from 'vitest'
import { riveActivityValue, riveMotionValue } from './rive-mascot-renderer'

describe('Rive mascot contract', () => {
  it('maps product activity without leaking input names to the page', () => {
    expect(riveActivityValue('idle')).toBe(0)
    expect(riveActivityValue('focused')).toBe(1)
    expect(riveActivityValue('submitting')).toBe(2)
  })

  it('maps all motion levels', () => {
    expect(riveMotionValue('full')).toBe(0)
    expect(riveMotionValue('reduced')).toBe(1)
    expect(riveMotionValue('static')).toBe(2)
  })
})
