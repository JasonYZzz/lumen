'use client'

import { useEffect, useRef, useState, type RefObject } from 'react'

export function ConversationScrollNav({ containerRef, items, onNavigate }: {
  containerRef: RefObject<HTMLElement | null>
  items: Array<{ id: string; text: string }>
  onNavigate: () => void
}) {
  const [active, setActive] = useState(0)
  const [preview, setPreview] = useState<number | null>(null)
  const [scrollable, setScrollable] = useState(false)
  const navRef = useRef<HTMLElement>(null)
  const ids = JSON.stringify(items.map((item) => item.id))

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const turnIds = new Set<string>(JSON.parse(ids))
    let frame = 0
    const measure = () => {
      frame = 0
      const rows = Array.from(container.querySelectorAll<HTMLElement>('[data-turn-id]'))
        .filter((row) => turnIds.has(row.dataset.turnId ?? ''))
      setScrollable(container.scrollHeight > container.clientHeight + 8)
      const top = container.getBoundingClientRect().top + 40
      let index = 0
      rows.forEach((row, position) => { if (row.getBoundingClientRect().top <= top) index = position })
      if (container.scrollHeight - container.scrollTop - container.clientHeight < 8) index = rows.length - 1
      setActive(Math.max(0, index))
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(measure) }
    const observer = new ResizeObserver(schedule)
    observer.observe(container)
    container.querySelectorAll('[data-turn-id]').forEach((row) => observer.observe(row))
    container.addEventListener('scroll', schedule, { passive: true })
    measure()
    return () => { observer.disconnect(); container.removeEventListener('scroll', schedule); cancelAnimationFrame(frame) }
  }, [containerRef, ids])

  const jump = (index: number) => {
    const container = containerRef.current
    const row = container && Array.from(container.querySelectorAll<HTMLElement>('[data-turn-id]'))
      .find((element) => element.dataset.turnId === items[index]?.id)
    if (!container || !row) return
    onNavigate()
    setActive(index)
    container.scrollTo({
      top: container.scrollTop + row.getBoundingClientRect().top - container.getBoundingClientRect().top - 16,
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    })
  }

  if (!scrollable || items.length < 2) return null
  return <nav ref={navRef} className="conversation-scroll-nav" aria-label="对话消息导航"
    onMouseLeave={() => setPreview(null)} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPreview(null)
    }}>
    <div className="conversation-scroll-ticks">
      {items.map((item, index) => <button key={item.id} type="button"
        className={active === index ? 'is-active' : ''} aria-current={active === index ? 'location' : undefined}
        aria-label={`跳到第 ${index + 1} 条消息：${item.text}`} tabIndex={active === index ? 0 : -1}
        onMouseEnter={() => setPreview(index)} onFocus={() => setPreview(index)} onClick={() => jump(index)}
        onKeyDown={(event) => {
          const target = event.key === 'ArrowDown' ? Math.min(items.length - 1, index + 1)
            : event.key === 'ArrowUp' ? Math.max(0, index - 1)
              : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : null
          if (target === null) return
          event.preventDefault()
          navRef.current?.querySelectorAll('button')[target]?.focus({ preventScroll: true })
          jump(target)
        }}><span aria-hidden="true" /></button>)}
    </div>
    {preview !== null && items[preview] && <div className="conversation-scroll-preview" aria-hidden="true">
      <small>消息 {preview + 1} / {items.length}</small><span>{items[preview].text}</span>
    </div>}
  </nav>
}
