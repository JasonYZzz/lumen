'use client'

import { Composer, type ComposerProps } from './composer'
import styles from './landing-entry.module.css'

export function LandingEntry(props: ComposerProps) {
  return (
    <section className={styles.entry} aria-label="开始新任务">
      <h2 className={styles.wordmark} lang="en">LUMEN</h2>
      <Composer {...props} />
    </section>
  )
}
