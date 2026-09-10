'use client'

import { gsap } from 'gsap'
import { useLayoutEffect, useRef } from 'react'

export function ThinkingOrb() {
  const iconRef = useRef<HTMLImageElement>(null)

  useLayoutEffect(() => {
    const icon = iconRef.current
    if (!icon) return

    const motion = gsap.context(() => {
      if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return

      gsap.set(icon, { rotation: -6, scaleX: 0.82, scaleY: 0.78, opacity: 0.72 })

      gsap.timeline({ repeat: -1 })
        .to(icon, {
          rotation: 7,
          scaleX: 1.06,
          scaleY: 0.94,
          opacity: 1,
          duration: 0.64,
          ease: 'power2.out',
        })
        .to(icon, {
          rotation: -9,
          scaleX: 0.9,
          scaleY: 1.06,
          opacity: 0.9,
          duration: 0.58,
          ease: 'sine.inOut',
        })
        .to(icon, {
          rotation: -6,
          scaleX: 0.82,
          scaleY: 0.78,
          opacity: 0.72,
          duration: 0.93,
          ease: 'power2.inOut',
        })
    }, icon)

    return () => motion.revert()
  }, [])

  return (
    <img
      className="thinking-orb"
      ref={iconRef}
      src="/lumen-claude-amber.svg"
      alt=""
      aria-hidden="true"
    />
  )
}
