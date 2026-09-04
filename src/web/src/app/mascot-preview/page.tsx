'use client'

import { useRef, useState } from 'react'
import { VideoMascotRenderer } from '@/components/mascot/video-mascot-renderer'
import { useMascotVisibility } from '@/components/mascot/mascot-visibility'
import { useMascotMotionPreference } from '@/components/mascot/mascot-motion-preference'
import type { MascotActivity } from '@/components/mascot/mascot-types'
import styles from './preview.module.css'

export default function MascotPreview() {
  const stage = useRef<HTMLDivElement>(null)
  const visible = useMascotVisibility(stage)
  const preference = useMascotMotionPreference()
  const [activity, setActivity] = useState<MascotActivity>('idle')
  const [interaction, setInteraction] = useState(0)
  const [paused, setPaused] = useState(false)
  const [large, setLarge] = useState(false)
  const [slow, setSlow] = useState(false)
  const [dark, setDark] = useState(false)
  const [still, setStill] = useState(false)
  return <main className={styles.page} data-dark={dark}>
    <header className={styles.header}><a href="/">LUMEN</a><span>宠物动效 · 连续动画验收</span></header>
    <section className={styles.scene}>
      <p className={styles.eyebrow}>A LITTLE COMPANY</p>
      <div className={styles.heading}><h1>想到什么，<br />就从这里开始。</h1>
        <div ref={stage} className={styles.pet} style={{ width: large ? 336 : 168, height: large ? 336 : 168 }}>
          <button type="button" aria-label="和九尾狐打招呼" onClick={() => setInteraction(value => value + 1)}>
            <VideoMascotRenderer activity={activity} active={visible} motionMode={still ? 'static' : preference.mode}
              paused={paused} playbackRate={slow ? .5 : 1} interaction={interaction} />
          </button>
        </div>
      </div>
      <textarea className={styles.composer} aria-label="试试输入，看看小狐狸的反应" placeholder="试试输入，看看小狐狸的反应…"
        onFocus={() => setActivity('focused')} onBlur={() => setActivity('idle')} />
      <p className={styles.hint}>轻点狐狸打招呼。它会完成当前动作，再自然回应；不会打断姿势或连续重播。</p>
    </section>
    <footer className={styles.controls}>
      <button type="button" aria-pressed={paused} onClick={() => setPaused(!paused)}>{paused ? '继续播放' : '暂停'}</button>
      <button type="button" aria-pressed={large} onClick={() => setLarge(!large)}>2× 放大</button>
      <button type="button" aria-pressed={slow} onClick={() => setSlow(!slow)}>0.5× 慢放</button>
      <button type="button" aria-pressed={dark} onClick={() => setDark(!dark)}>深色背景</button>
      <button type="button" aria-pressed={still} onClick={() => setStill(!still)}>静态模式</button>
    </footer>
  </main>
}
