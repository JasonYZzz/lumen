'use client'

import { useEffect, useRef, useState } from 'react'
import { EMPTY_WORKSPACE_PROMPT } from '@/lib/copy'
import { Composer, type ComposerProps } from './composer'
import { AppearanceControl } from './appearance-control'
import { MascotScene } from './mascot/mascot-scene'
import type { MascotActivity } from './mascot/mascot-types'
import styles from './landing-entry.module.css'

export function LandingEntry({ stage = 0, onAppearanceChange, appearanceNote, ...props }: ComposerProps & {
  stage?: number
  onAppearanceChange: (stage: number) => void
  appearanceNote?: string
}) {
  const [activity, setActivity] = useState<MascotActivity>('idle')
  const submitTimer = useRef<number | null>(null)

  useEffect(() => () => {
    if (submitTimer.current) window.clearTimeout(submitTimer.current)
  }, [])

  const handleSubmit = () => {
    setActivity('submitting')
    if (submitTimer.current) window.clearTimeout(submitTimer.current)
    submitTimer.current = window.setTimeout(() => setActivity('idle'), 1_000)
    props.onSubmit()
  }

  return (
    <section className={styles.entry} aria-label="开始新任务">
      <div className={styles.intro}>
        <MascotScene activity={activity} stage={stage} className={styles.mascot} />
        <p className={styles.title}>{EMPTY_WORKSPACE_PROMPT}</p>
        <p className={styles.companion}>陪你从一个想法开始</p>
      </div>
      <Composer
        {...props}
        onSubmit={handleSubmit}
        onFocusChange={(focused) => setActivity(focused ? 'focused' : 'idle')}
      />
      <AppearanceControl stage={stage} onChange={onAppearanceChange} note={appearanceNote} />
    </section>
  )
}
