'use client'

import { useEffect, useRef, useState } from 'react'
import { useMascotMotionPreference } from './mascot-motion-preference'
import type { MascotSceneProps } from './mascot-types'
import { useMascotVisibility } from './mascot-visibility'
import { foxImage } from './fox-growth'
import styles from './mascot.module.css'

export function MascotScene({ activity, stage = 0, motionMode, className }: MascotSceneProps) {
  const element = useRef<HTMLDivElement>(null)
  const active = useMascotVisibility(element)
  const { mode } = useMascotMotionPreference(motionMode)
  const [greeting, setGreeting] = useState(false)
  const [failedSource, setFailedSource] = useState<string | null>(null)
  const [blinkReady, setBlinkReady] = useState(false)
  const [portrait, setPortrait] = useState<{ current: number; previous: number | null }>({current: stage, previous: null})
  const canMove = active && mode === 'full'
  useEffect(() => {
    const target = foxImage(stage)
    if (!active || stage === portrait.current || failedSource === target) return
    const image = new Image()
    image.sizes = '(max-width: 640px) 168px, 240px'
    image.srcset = `${target} 480w, ${foxImage(stage, 'retina')} 960w`
    image.onload = () => {
      setFailedSource(null)
      setPortrait(current => ({current: stage,
        previous: canMove ? failedSource === foxImage(current.current) ? 0 : current.current : null}))
    }
    image.onerror = () => setFailedSource(target)
    image.src = target
    return () => { image.onload = null; image.onerror = null }
  }, [stage, portrait.current, active, canMove, failedSource])
  useEffect(() => {
    if (portrait.previous === null) return
    if (!canMove) { setPortrait(current => ({...current, previous: null})); return }
    const timer = window.setTimeout(() => setPortrait(current => ({...current, previous: null})), 320)
    return () => window.clearTimeout(timer)
  }, [portrait.current, portrait.previous, canMove])
  useEffect(() => {
    if (!greeting) return
    const timer = window.setTimeout(() => setGreeting(false), 700)
    return () => window.clearTimeout(timer)
  }, [greeting])
  const source = foxImage(portrait.current)
  return <div ref={element} className={`${styles.stage} ${className ?? ''}`}
    data-stage={portrait.current + 1} data-motion={mode} data-active={active} data-activity={activity}>
    <button type="button" className={styles.petButton} aria-label="和九尾狐打招呼"
      onClick={() => setGreeting(true)} data-greeting={greeting}
      onPointerMove={event => {
        if (!canMove || event.pointerType === 'touch') return
        const bounds = event.currentTarget.getBoundingClientRect()
        const lean = ((event.clientX - bounds.left) / bounds.width - .5) * 5
        event.currentTarget.style.setProperty('--fox-lean', `${lean}deg`)
      }}
      onPointerLeave={event => event.currentTarget.style.removeProperty('--fox-lean')}>
      <span className={styles.follow}><span className={styles.pose}><span className={styles.life}>
        {canMove && portrait.previous !== null && <img className={styles.previous} src={foxImage(portrait.previous)}
          srcSet={`${foxImage(portrait.previous)} 480w, ${foxImage(portrait.previous, 'retina')} 960w`}
          sizes="(max-width: 640px) 168px, 240px" width={1254} height={1254} alt="" draggable={false} />}
        <img key={source} className={`${styles.character} ${canMove && portrait.previous !== null ? styles.entering : ''}`}
          src={failedSource === source ? foxImage(0) : source}
          srcSet={failedSource === source ? undefined : `${source} 480w, ${foxImage(portrait.current, 'retina')} 960w`}
          sizes="(max-width: 640px) 168px, 240px" width={1254} height={1254}
          alt="" draggable={false} onError={() => setFailedSource(source)} />
        {canMove && portrait.current === 0 && portrait.previous === null && failedSource !== source && <img className={styles.blink}
          data-ready={blinkReady} src="/new-design/fox-stage-01-v2-blink.webp" width={960} height={960}
          alt="" draggable={false} onLoad={() => setBlinkReady(true)} onError={() => setBlinkReady(false)} />}
      </span></span></span>
    </button>
    <span className="sr-only" role="status">{greeting ? '小狐狸向你打了个招呼' : ''}</span>
  </div>
}
