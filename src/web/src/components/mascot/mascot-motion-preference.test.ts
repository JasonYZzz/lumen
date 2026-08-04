import { describe, expect, it } from 'vitest'
import { resolveMascotMotionMode } from './mascot-motion-preference'

describe('mascot motion preference', () => {
  it('prioritizes an explicit embed mode', () => {
    expect(resolveMascotMotionMode({
      explicitMode: 'full',
      systemReduced: true,
    })).toBe('full')
  })

  it('uses reduced motion when requested by the system', () => {
    expect(resolveMascotMotionMode({ systemReduced: true })).toBe('reduced')
  })

  it('defaults to full motion', () => {
    expect(resolveMascotMotionMode({ systemReduced: false })).toBe('full')
  })
})
