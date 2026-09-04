import { describe, expect, it } from 'vitest'
import { MascotPlayback } from './mascot-playback'

describe('authored mascot playback', () => {
  it('loops idle without introducing still poses', () => {
    const player = new MascotPlayback()
    expect(player.end()).toBe('idle')
  })
  it('queues attention until a shared rest pose', () => {
    const player = new MascotPlayback()
    player.setActivity('focused')
    expect(player.consumeAtRest(3)).toBeNull()
    expect(player.clip).toBe('idle')
    expect(player.end()).toBe('react')
    expect(player.end()).toBe('idle')
  })
  it('responds immediately when already at rest', () => {
    const player = new MascotPlayback()
    player.requestReaction()
    expect(player.consumeAtRest(.1)).toBe('react')
  })
  it('coalesces clicks and does not restart or endlessly replay a response', () => {
    const player = new MascotPlayback()
    player.requestReaction()
    player.requestReaction()
    expect(player.end()).toBe('react')
    player.requestReaction()
    player.setActivity('submitting')
    expect(player.end()).toBe('idle')
    expect(player.end()).toBe('idle')
  })
  it('does not retrigger on every render or typed character', () => {
    const player = new MascotPlayback()
    player.setActivity('focused')
    player.end()
    player.end()
    player.setActivity('focused')
    expect(player.pending).toBe(false)
    player.setActivity('idle')
    player.setActivity('focused')
    expect(player.pending).toBe(true)
  })
})
