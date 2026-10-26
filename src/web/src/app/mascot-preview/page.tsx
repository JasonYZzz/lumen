'use client'

import { useState } from 'react'
import { AppearanceControl } from '@/components/appearance-control'
import { MascotScene } from '@/components/mascot/mascot-scene'
import { FOX_APPEARANCES, foxImage } from '@/components/mascot/fox-growth'
import styles from './preview.module.css'

export default function MascotPreview() {
  const [stage, setStage] = useState(0)
  const [still, setStill] = useState(false)
  return <main className={styles.page} data-appearance={FOX_APPEARANCES[stage].id}>
    <header><a href="/">返回 Lumen</a><span>形象预览</span></header>
    <section className={styles.preview} aria-label="狐狸形象预览">
      <MascotScene stage={stage} activity="idle" motionMode={still ? 'static' : undefined} />
      <AppearanceControl stage={stage} onChange={setStage} note="此页仅预览，不会更改已保存的外观。" />
    </section>
    <footer><button type="button" aria-pressed={still} onClick={() => setStill(!still)}>静态模式</button>
      <a href={foxImage(stage, 'full')} download>下载高清 WebP</a>
    </footer>
  </main>
}
