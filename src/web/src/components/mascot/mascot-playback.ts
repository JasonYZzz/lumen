import type { MascotActivity } from './mascot-types'

export type MascotClip = 'idle' | 'react'

/** One playback authority. Finish each authored movement before changing clips. */
export class MascotPlayback {
  clip: MascotClip = 'idle'
  pending = false
  private activity: MascotActivity = 'idle'

  setActivity(activity: MascotActivity) {
    if (activity !== this.activity && activity !== 'idle') this.requestReaction()
    this.activity = activity
  }

  requestReaction() {
    // Coalesce repeated focus/clicks; never restart a gesture midway through it.
    if (this.clip === 'idle') this.pending = true
  }

  consumeAtRest(currentTime: number): MascotClip | null {
    if (this.clip === 'idle' && this.pending && currentTime < .25) {
      this.pending = false
      this.clip = 'react'
      return this.clip
    }
    return null
  }

  end(): MascotClip {
    this.clip = this.clip === 'idle' && this.pending ? 'react' : 'idle'
    this.pending = false
    return this.clip
  }
}
