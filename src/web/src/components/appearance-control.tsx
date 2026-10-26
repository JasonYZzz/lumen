'use client'

import { useEffect, useId, useState } from 'react'
import { Palette } from '@phosphor-icons/react'
import { FOX_APPEARANCES, foxStageForAppearance } from './mascot/fox-growth'
import styles from './appearance-control.module.css'

const storageKey = 'lumen.appearance'

export function useAppearance() {
  const [stage, setStage] = useState(0)
  const [storageAvailable, setStorageAvailable] = useState(true)
  useEffect(() => {
    try { setStage(foxStageForAppearance(localStorage.getItem(storageKey))) }
    catch { setStorageAvailable(false) }
    const sync = (event: StorageEvent) => {
      if (event.key === storageKey || event.key === null) setStage(foxStageForAppearance(event.newValue))
    }
    window.addEventListener('storage', sync)
    return () => { window.removeEventListener('storage', sync); delete document.documentElement.dataset.appearance }
  }, [])
  useEffect(() => { document.documentElement.dataset.appearance = FOX_APPEARANCES[stage].id }, [stage])
  const selectStage = (next: number) => {
    if (!Number.isInteger(next) || next < 0 || next >= FOX_APPEARANCES.length) return
    setStage(next)
    try { localStorage.setItem(storageKey, FOX_APPEARANCES[next].id); setStorageAvailable(true) }
    catch { setStorageAvailable(false) }
  }
  return { stage, selectStage, storageAvailable }
}

export function AppearanceControl({ stage, onChange, note }: {
  stage: number
  onChange: (stage: number) => void
  note?: string
}) {
  const id = useId()
  const appearance = FOX_APPEARANCES[stage]
  return <div className={styles.control}>
    <div className={styles.row}>
      <label className={styles.label} htmlFor={`${id}-range`}>
        <Palette size={15} aria-hidden="true" />外观
      </label>
      <div className={styles.slider}>
        <div className={styles.track} aria-hidden="true">
          {FOX_APPEARANCES.map((item, index) => <span key={item.id} data-selected={index === stage}
            style={{ backgroundColor: `var(--appearance-${item.id})` }} />)}
        </div>
        <input id={`${id}-range`} className={styles.range} type="range" min={0} max={4} step={1} value={stage}
          aria-label="背景与成长档位" aria-valuetext={`${appearance.label}背景，形象 ${stage + 1} / 5`}
          aria-describedby={`${id}-hint${note ? ` ${id}-note` : ''}`}
          onChange={event => onChange(Number(event.currentTarget.value))} />
      </div>
      <output htmlFor={`${id}-range`} className={styles.value}>{appearance.label}</output>
    </div>
    <span id={`${id}-hint`} className="sr-only">拖动切换五种浅色背景，小狐狸随档位成长。支持左右方向键。</span>
    {note && <p id={`${id}-note`} className={styles.note}>{note}</p>}
  </div>
}
