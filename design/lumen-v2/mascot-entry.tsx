import { createRoot, type Root } from '../../src/web/node_modules/react-dom/client'
import { useEffect, useRef, useState } from '../../src/web/node_modules/react'
import { VideoMascotRenderer } from '../../src/web/src/components/mascot/video-mascot-renderer'
import { useMascotMotionPreference } from '../../src/web/src/components/mascot/mascot-motion-preference'
import { useMascotVisibility } from '../../src/web/src/components/mascot/mascot-visibility'
import type { MascotActivity } from '../../src/web/src/components/mascot/mascot-types'

function ExistingMascot() {
  const stage = useRef<HTMLDivElement>(null)
  const active = useMascotVisibility(stage)
  const { mode } = useMascotMotionPreference()
  const [activity, setActivity] = useState<MascotActivity>('idle')
  const [interaction, setInteraction] = useState(0)
  useEffect(() => {
    const focus = (event: FocusEvent) => {
      if ((event.target as HTMLElement)?.id === 'prompt') setActivity('focused')
    }
    const blur = (event: FocusEvent) => {
      if ((event.target as HTMLElement)?.id === 'prompt') setActivity('idle')
    }
    document.addEventListener('focusin', focus)
    document.addEventListener('focusout', blur)
    return () => {
      document.removeEventListener('focusin', focus)
      document.removeEventListener('focusout', blur)
    }
  }, [])
  return <div className="existing-mascot" ref={stage} data-motion={mode}>
    <button type="button" aria-label="和九尾狐打招呼" onClick={() => setInteraction(value => value + 1)}>
      <VideoMascotRenderer activity={activity} motionMode={mode} active={active} interaction={interaction} />
    </button>
  </div>
}

const roots = new Map<Element, Root>()
window.LumenMascot = {
  mountAll() {
    document.querySelectorAll('[data-mascot]').forEach(element => {
      if (roots.has(element)) return
      const root = createRoot(element)
      roots.set(element, root)
      root.render(<ExistingMascot />)
    })
  },
  dispose() {
    roots.forEach(root => root.unmount())
    roots.clear()
  },
}

declare global {
  interface Window {
    LumenMascot: { mountAll(): void; dispose(): void }
  }
}
