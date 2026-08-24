'use client'

import { Archive, ArrowCounterClockwise, PencilSimpleLine, Trash } from '@phosphor-icons/react'
import type { KeyboardEvent } from 'react'
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import type { SessionSummary } from '@/lib/api/types'
import { useAnchoredPopover } from './use-anchored-popover'

export function SessionActionsMenu({
  anchor,
  session,
  busy,
  blocked,
  onClose,
  onRename,
  onArchive,
  onDelete,
}: {
  anchor: HTMLElement
  session: SessionSummary
  busy: boolean
  blocked: boolean
  onClose: () => void
  onRename: () => void
  onArchive: () => void
  onDelete: () => void
}) {
  const menuRef = useRef<HTMLDivElement>(null)
  const position = useAnchoredPopover({
    open: true,
    anchor,
    popoverRef: menuRef,
    width: 218,
    placement: 'side',
  })

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (!anchor.contains(target) && !menuRef.current?.contains(target)) onClose()
    }
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        anchor.focus()
      }
    }
    window.addEventListener('pointerdown', handlePointerDown, true)
    window.addEventListener('keydown', handleKeyDown)
    const frame = window.requestAnimationFrame(() => {
      menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
    })
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('pointerdown', handlePointerDown, true)
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [anchor, onClose])

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])
    if (items.length === 0) return
    const current = Math.max(0, items.indexOf(document.activeElement as HTMLButtonElement))
    let next = current
    if (event.key === 'ArrowDown') next = (current + 1) % items.length
    else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = items.length - 1
    else if (event.key === 'Tab') {
      onClose()
      return
    } else return
    event.preventDefault()
    items[next]?.focus()
  }

  return createPortal(
    <div
      ref={menuRef}
      className="session-actions-popover"
      role="menu"
      aria-label={`管理任务：${session.title}`}
      style={position}
      onKeyDown={handleMenuKeyDown}
    >
      <button type="button" role="menuitem" onClick={onRename}>
        <PencilSimpleLine size={17} aria-hidden="true" />
        <span><strong>重命名</strong><small>修改任务标题</small></span>
      </button>
      <button type="button" role="menuitem" disabled={busy || blocked} onClick={onArchive}>
        {session.archived
          ? <ArrowCounterClockwise size={17} aria-hidden="true" />
          : <Archive size={17} aria-hidden="true" />}
        <span>
          <strong>{session.archived ? '恢复任务' : '归档任务'}</strong>
          <small>{session.archived ? '移回最近任务' : '保留记录并移出最近列表'}</small>
        </span>
      </button>
      <div className="session-actions-divider" role="separator" />
      <button
        type="button"
        role="menuitem"
        className="is-danger"
        disabled={busy || blocked}
        onClick={onDelete}
      >
        <Trash size={17} aria-hidden="true" />
        <span><strong>删除任务</strong><small>从客户端列表中移除</small></span>
      </button>
    </div>,
    document.body,
  )
}
