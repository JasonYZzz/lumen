// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ConversationScrollNav } from './conversation-scroll-nav'

let root: Root
let surface: HTMLDivElement
let timeline: HTMLDivElement
const navigate = vi.fn()
const items = [{ id: 'first', text: '第一条消息' }, { id: 'second', text: '第二条消息' }]

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('matchMedia', () => ({ matches: true }))
  surface = document.createElement('div')
  timeline = document.createElement('div')
  timeline.innerHTML = '<section data-turn-id="first"></section><section data-turn-id="second"></section>'
  Object.defineProperties(timeline, { scrollHeight: { value: 1800, configurable: true }, clientHeight: { value: 600 } })
  timeline.getBoundingClientRect = () => ({ top: 60 } as DOMRect)
  Array.from(timeline.children).forEach((row, i) => {
    row.getBoundingClientRect = () => ({ top: 86 + 800 * i - timeline.scrollTop } as DOMRect)
  })
  timeline.scrollTo = vi.fn()
  document.body.append(timeline, surface)
  root = createRoot(surface)
  navigate.mockReset()
})
afterEach(async () => {
  await act(async () => root.unmount())
  surface.remove(); timeline.remove()
  vi.unstubAllGlobals()
})

it('jumps to the actual message, disengages live following, and supports keyboard navigation', async () => {
  await act(async () => root.render(<ConversationScrollNav containerRef={{ current: timeline }} items={items} onNavigate={navigate} />))
  const buttons = surface.querySelectorAll('button')
  expect(buttons[0].getAttribute('aria-current')).toBe('location')
  await act(async () => buttons[1].click())
  expect(navigate).toHaveBeenCalledOnce()
  expect(timeline.scrollTo).toHaveBeenCalledWith({ top: 810, behavior: 'auto' })
  await act(async () => buttons[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })))
  expect(document.activeElement).toBe(buttons[0])
  expect(timeline.scrollTo).toHaveBeenLastCalledWith({ top: 10, behavior: 'auto' })
  expect(surface.textContent).toContain('第一条消息')
})

it('does not show position controls when the conversation fits the viewport', async () => {
  Object.defineProperty(timeline, 'scrollHeight', { value: 600 })
  await act(async () => root.render(<ConversationScrollNav containerRef={{ current: timeline }} items={items} onNavigate={navigate} />))
  expect(surface.querySelector('nav')).toBeNull()
})
