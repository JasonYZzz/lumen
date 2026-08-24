'use client'

import type { CSSProperties, RefObject } from 'react'
import { useCallback, useEffect, useLayoutEffect, useState } from 'react'

type PopoverPlacement = 'vertical' | 'side'

interface AnchoredPopoverOptions {
  open: boolean
  anchor: HTMLElement | null
  popoverRef: RefObject<HTMLElement | null>
  width: number
  placement?: PopoverPlacement
  gap?: number
}

export function useAnchoredPopover({
  open,
  anchor,
  popoverRef,
  width,
  placement = 'vertical',
  gap = 8,
}: AnchoredPopoverOptions): CSSProperties {
  const [style, setStyle] = useState<CSSProperties>({ visibility: 'hidden' })

  const updatePosition = useCallback(() => {
    const popover = popoverRef.current
    if (!open || !anchor || !popover) return

    const viewportWidth = window.innerWidth
    const viewportHeight = window.innerHeight
    const safeWidth = Math.min(width, viewportWidth - 24)
    const anchorRect = anchor.getBoundingClientRect()
    const measuredHeight = Math.min(popover.scrollHeight, viewportHeight - 24)
    let left: number
    let top: number

    if (placement === 'side') {
      const fitsRight = anchorRect.right + gap + safeWidth <= viewportWidth - 12
      const fitsLeft = anchorRect.left - gap - safeWidth >= 12
      if (fitsRight || fitsLeft) {
        left = fitsRight ? anchorRect.right + gap : anchorRect.left - gap - safeWidth
        top = Math.min(
          Math.max(12, anchorRect.top - 8),
          Math.max(12, viewportHeight - measuredHeight - 12),
        )
      } else {
        const roomBelow = viewportHeight - anchorRect.bottom - 12
        const openAbove = roomBelow < measuredHeight && anchorRect.top - 12 > roomBelow
        left = Math.min(Math.max(12, anchorRect.right - safeWidth), viewportWidth - safeWidth - 12)
        top = openAbove
          ? Math.max(12, anchorRect.top - measuredHeight - gap)
          : Math.min(anchorRect.bottom + gap, viewportHeight - measuredHeight - 12)
      }
    } else {
      const roomBelow = viewportHeight - anchorRect.bottom - 12
      const roomAbove = anchorRect.top - 12
      const openAbove = roomBelow < Math.min(measuredHeight, 240) && roomAbove > roomBelow
      left = Math.min(Math.max(12, anchorRect.left), viewportWidth - safeWidth - 12)
      top = openAbove
        ? Math.max(12, anchorRect.top - measuredHeight - gap)
        : Math.min(anchorRect.bottom + gap, viewportHeight - measuredHeight - 12)
    }

    setStyle({
      top,
      left,
      width: safeWidth,
      maxHeight: viewportHeight - 24,
      visibility: 'visible',
    })
  }, [anchor, gap, open, placement, popoverRef, width])

  useLayoutEffect(() => {
    if (!open) {
      setStyle({ visibility: 'hidden' })
      return
    }

    updatePosition()
  }, [open, updatePosition])

  useEffect(() => {
    if (!open) return
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [open, updatePosition])

  return style
}
