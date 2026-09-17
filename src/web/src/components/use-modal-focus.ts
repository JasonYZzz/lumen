import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * 模态浮层焦点管理（配合 role="dialog" aria-modal 使用）：
 * - 挂载时聚焦浮层内首个可交互控件（浮层已通过 autoFocus 聚焦时尊重现状）
 * - Escape 触发 onClose
 * - Tab / Shift+Tab 将焦点循环限制在浮层内
 * - 卸载后焦点归还触发元素（触发元素已不在文档中时跳过）
 *
 * 返回的 ref 需挂在浮层容器上，容器建议带 tabIndex={-1} 作为无控件时的聚焦兜底。
 */
export function useModalFocus(
  onClose: () => void,
  enabled = true,
  returnFocusRef?: RefObject<HTMLElement | null>,
): RefObject<HTMLDivElement | null> {
  const containerRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!enabled) return
    const container = containerRef.current
    if (!container) return
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null

    const visibleFocusable = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter((element) => element.getClientRects().length > 0)

    if (!container.contains(document.activeElement)) {
      const initial = visibleFocusable()[0]
      if (initial) initial.focus()
      else container.focus()
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      // A source panel may open above a report; only the top dialog owns keyboard focus.
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]')
      if (dialogs.length && dialogs[dialogs.length - 1] !== container) return
      if (event.key === 'Escape' && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault()
        event.stopPropagation()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = visibleFocusable()
      if (items.length === 0) {
        event.preventDefault()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      const focusOutside = !active || !container.contains(active)
      if (event.shiftKey && (active === first || focusOutside)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || focusOutside)) {
        event.preventDefault()
        first.focus()
      }
    }

    // capture 阶段处理，避免浮层打开时 Escape/Tab 泄漏到页面其余快捷键
    document.addEventListener('keydown', handleKeyDown, true)
    return () => {
      document.removeEventListener('keydown', handleKeyDown, true)
      // A mobile drawer can disappear while handing focus to another dialog.
      const returnTarget = returnFocusRef?.current ?? trigger
      if (returnTarget && document.contains(returnTarget)) returnTarget.focus({ preventScroll: true })
    }
  }, [enabled, returnFocusRef])

  return containerRef
}
