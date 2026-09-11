'use client'

import { CaretDown, Check, CircleNotch } from '@phosphor-icons/react'
import type { KeyboardEvent, ReactNode } from 'react'
import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useAnchoredPopover } from './use-anchored-popover'

export interface ChoiceOption<T extends string> {
  value: T
  label: string
  compactLabel?: string
  description?: string
  icon?: ReactNode
}

export function ChoiceMenu<T extends string>({
  label,
  value,
  options,
  icon,
  disabled = false,
  menuWidth = 292,
  className = '',
  open: controlledOpen,
  onOpenChange,
  searchable = false,
  alignToComposer = false,
  description,
  onChange,
}: {
  label: string
  value: T
  options: Array<ChoiceOption<T>>
  icon?: ReactNode
  disabled?: boolean
  menuWidth?: number
  className?: string
  open?: boolean
  onOpenChange?: (open: boolean) => void
  searchable?: boolean
  alignToComposer?: boolean
  description?: string
  onChange: (value: T) => void | Promise<void>
}) {
  const [localOpen, setLocalOpen] = useState(false)
  const open = controlledOpen ?? localOpen
  const setOpen = (next: boolean) => { setLocalOpen(next); onOpenChange?.(next) }
  const [query, setQuery] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const menuId = useId()
  const selected = options.find((option) => option.value === value)
  const matches = options.filter((option) => `${option.label} ${option.description ?? ''}`.toLowerCase().includes(query.trim().toLowerCase()))
  const position = useAnchoredPopover({
    open,
    anchor: alignToComposer
      ? triggerRef.current?.closest('.composer-shell')?.querySelector<HTMLElement>('.composer') ?? triggerRef.current
      : triggerRef.current,
    popoverRef: menuRef,
    width: menuWidth,
    matchAnchorWidth: alignToComposer,
  })

  const close = (restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }))
  }

  useEffect(() => {
    if (!open) return
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) close()
    }
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        close(true)
      }
    }
    window.addEventListener('pointerdown', handlePointerDown, true)
    window.addEventListener('keydown', handleEscape)
    return () => {
      window.removeEventListener('pointerdown', handlePointerDown, true)
      window.removeEventListener('keydown', handleEscape)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    setQuery('')
    setError(null)
    const frame = window.requestAnimationFrame(() => {
      const target = searchRef.current ?? menuRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
        ?? menuRef.current?.querySelector<HTMLElement>('[role="option"]')
      target?.focus({ preventScroll: true })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [open])

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])
    if (items.length === 0) return
    const current = items.indexOf(document.activeElement as HTMLButtonElement)
    let next = current
    if (event.key === 'ArrowDown') next = (current + 1) % items.length
    else if (event.key === 'ArrowUp') next = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
    else if (event.key === 'Home' && current >= 0) next = 0
    else if (event.key === 'End' && current >= 0) next = items.length - 1
    else if (event.key === 'Tab') {
      event.preventDefault()
      close(true)
      return
    } else if (event.key === 'Enter' && document.activeElement === searchRef.current) {
      event.preventDefault()
      items[0]?.click()
      return
    } else return
    event.preventDefault()
    items[next]?.focus()
  }

  return (
    <div className={`choice-menu ${className}`.trim()}>
      <button
        ref={triggerRef}
        type="button"
        className="choice-menu-trigger"
        disabled={disabled}
        title={description}
        aria-label={`${label}：${selected?.label ?? (value || '未配置')}`}
        aria-haspopup="listbox"
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault()
            setOpen(true)
          }
        }}
      >
        {icon && <span className="choice-menu-icon" aria-hidden="true">{icon}</span>}
        <span className="choice-menu-value">{selected?.compactLabel ?? selected?.label ?? (value || '未配置')}</span>
        <CaretDown className="choice-menu-caret" size={12} weight="bold" aria-hidden="true" />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className={`choice-menu-popover ${alignToComposer ? 'is-model-picker' : ''}`}
          style={position}
          onKeyDown={handleMenuKeyDown}
        >
          {!searchable && <div className="choice-menu-caption">{label}</div>}
          {searchable && <label className="choice-menu-search">
            <input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索" aria-label={`搜索${label}`} aria-controls={menuId} />
          </label>}
          <div id={menuId} role="listbox" aria-label={label} aria-busy={pending}>
          {matches.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              tabIndex={-1}
              disabled={pending || disabled}
              className="choice-menu-option"
              onClick={async () => {
                if (pending || disabled) return
                setPending(true)
                setError(null)
                try {
                  if (option.value !== value) await onChange(option.value)
                  close(true)
                } catch (error) {
                  setError(error instanceof Error ? error.message : '切换失败，请重试')
                } finally {
                  setPending(false)
                }
              }}
            >
              {(option.icon || (alignToComposer && icon)) && <span className="choice-option-icon" aria-hidden="true">{option.icon ?? icon}</span>}
              <span>
                <strong>{option.label}</strong>
                {option.description && <small>{option.description}</small>}
              </span>
              {option.value === value && <Check size={16} weight="bold" aria-hidden="true" />}
            </button>
          ))}
          </div>
          {!matches.length && <p className="choice-menu-empty">{options.length ? '没有匹配项，请尝试其他名称' : '暂无可用选项'}</p>}
          {(description || pending) && <p className="choice-menu-description" role="status">{pending ? <><CircleNotch size={14} className="spin" /> 正在切换…</> : description}</p>}
          {error && <p className="choice-menu-error" role="alert">{error}</p>}
        </div>,
        document.body,
      )}
    </div>
  )
}
