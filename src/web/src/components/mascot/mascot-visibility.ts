import { useEffect, useState, type RefObject } from 'react'

export function useMascotVisibility(target: RefObject<HTMLElement | null>) {
  const [pageVisible, setPageVisible] = useState(true)
  const [onScreen, setOnScreen] = useState(true)

  useEffect(() => {
    const syncPageVisibility = () => setPageVisible(!document.hidden)
    syncPageVisibility()
    document.addEventListener('visibilitychange', syncPageVisibility)
    return () => document.removeEventListener('visibilitychange', syncPageVisibility)
  }, [])

  useEffect(() => {
    const element = target.current
    if (!element || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      ([entry]) => setOnScreen(entry.isIntersecting),
      { threshold: 0.05 },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [target])

  return pageVisible && onScreen
}
