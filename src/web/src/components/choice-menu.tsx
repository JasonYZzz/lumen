'use client'

import { CaretDown, Check } from '@phosphor-icons/react'
import type { KeyboardEvent, ReactNode } from 'react'
import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useAnchoredPopover } from './use-anchored-popover'

export interface ChoiceOption<T extends string> {
  value: T
  label: string
  description?: string
}

export function ChoiceMenu<T extends string>({
  label,
  value,
  options,
  icon,
  disabled = false,
  menuWidth = 292,
  className = '',
  onChange,
}: {
  label: string
  value: T
  options: Array<ChoiceOption<T>>
  icon?: ReactNode
  disabled?: boolean
  menuWidth?: number
  className?: string
  onChange: (value: T) => void
}) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuId = useId()
  const selected = options.find((option) => option.value === value) ?? options[0]
  const position = useAnchoredPopover({
    open,
    anchor: triggerRef.current,
    popoverRef: menuRef,
    width: menuWidth,
  })

  const close = (restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus())
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
    const frame = window.requestAnimationFrame(() => {
      const selectedOption = menuRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
      selectedOption?.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [open])

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])
    if (items.length === 0) return
    const current = Math.max(0, items.indexOf(document.activeElement as HTMLButtonElement))
    let next = current
    if (event.key === 'ArrowDown') next = (current + 1) % items.length
    else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = items.length - 1
    else if (event.key === 'Tab') {
      close()
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
        aria-label={`${label}：${selected?.label ?? value}`}
        aria-haspopup="listbox"
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault()
            setOpen(true)
          }
        }}
      >
        {icon && <span className="choice-menu-icon" aria-hidden="true">{icon}</span>}
        <span className="choice-menu-value">{selected?.label ?? value}</span>
        <CaretDown className="choice-menu-caret" size={12} weight="bold" aria-hidden="true" />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          id={menuId}
          className="choice-menu-popover"
          role="listbox"
          aria-label={label}
          style={position}
          onKeyDown={handleMenuKeyDown}
        >
          <div className="choice-menu-caption">{label}</div>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              tabIndex={-1}
              className="choice-menu-option"
              onClick={() => {
                if (option.value !== value) onChange(option.value)
                close(true)
              }}
            >
              <span>
                <strong>{option.label}</strong>
                {option.description && <small>{option.description}</small>}
              </span>
              {option.value === value && <Check size={16} weight="bold" aria-hidden="true" />}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  )
}
