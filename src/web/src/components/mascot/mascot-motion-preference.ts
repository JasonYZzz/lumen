import { useEffect, useState } from 'react'
import type { MascotMotionMode } from './mascot-types'

export function resolveMascotMotionMode({
  explicitMode,
  systemReduced,
}: {
  explicitMode?: MascotMotionMode
  systemReduced: boolean
}): MascotMotionMode {
  if (explicitMode) return explicitMode
  if (systemReduced) return 'reduced'
  return 'full'
}

export function useMascotMotionPreference(explicitMode?: MascotMotionMode) {
  const [systemReduced, setSystemReduced] = useState(false)

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const syncSystemPreference = () => setSystemReduced(media.matches)
    syncSystemPreference()
    media.addEventListener('change', syncSystemPreference)
    return () => media.removeEventListener('change', syncSystemPreference)
  }, [])

  return {
    mode: resolveMascotMotionMode({ explicitMode, systemReduced }),
  }
}
