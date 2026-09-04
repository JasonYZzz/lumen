// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SessionSummary } from '@/lib/api/types'
import { SessionSearchDialog } from './session-search-dialog'

let root: Root
let container: HTMLDivElement
let sessions: SessionSummary[]
const onClose = vi.fn()
const onSelect = vi.fn()
const input = () => container.querySelector<HTMLInputElement>('input')!
const options = () => Array.from(container.querySelectorAll<HTMLElement>('[role="option"]'))
async function render(loading = false) {
  await act(async () => { root.render(<SessionSearchDialog sessions={sessions} loading={loading}
    onClose={onClose} onSelect={onSelect} />) })
}
async function type(value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input(), value)
    input().dispatchEvent(new Event('input', { bubbles: true }))
  })
}
async function key(key: string, isComposing = false) {
  await act(async () => { input().dispatchEvent(new KeyboardEvent('keydown', { key, isComposing, bubbles: true })) })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
  HTMLElement.prototype.scrollIntoView = vi.fn()
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
  sessions = ['架构分享 PPT', '中文说明', '旧架构'].map((title, index) => ({
    sessionId: String(index), title, archived: index === 2, modelId: 'test', createdAt: '2026-09-03T00:00:00Z',
  }))
})
afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('conversation search dialog', () => {
  it('shows recent active conversations and finds archived titles with trimmed, case-insensitive input', async () => {
    await render()
    expect(document.activeElement).toBe(input())
    expect(options().map((item) => item.textContent)).toEqual(['架构分享 PPT', '中文说明'])
    await type('  ppt  ')
    expect(options()).toHaveLength(1)
    await type('架构')
    expect(options().map((item) => item.textContent)).toEqual(['架构分享 PPT', '旧架构已归档'])
    await act(async () => options()[1].click())
    expect(onSelect).toHaveBeenCalledWith('2')
  })

  it('supports keyboard selection while IME confirmation and Escape keep the dialog open', async () => {
    await render()
    await key('ArrowUp')
    expect(document.getElementById(input().getAttribute('aria-activedescendant')!)?.textContent).toBe('中文说明')
    await key('ArrowDown')
    await key('Enter', true)
    await key('Escape', true)
    expect(onSelect).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    await key('ArrowDown')
    await key('Enter')
    expect(onSelect).toHaveBeenCalledWith('1')
    expect(document.activeElement).toBe(input())
    await type('不存在')
    await key('Enter')
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(input().hasAttribute('aria-activedescendant')).toBe(false)
    expect(container.textContent).toContain('没有找到相关对话')
  })

  it('keeps selection attached to the session when asynchronous titles or catalog order change', async () => {
    await render()
    await key('ArrowDown')
    sessions = [{ ...sessions[1] }, { ...sessions[0], title: '新生成的标题' }]
    await render()
    await key('Enter')
    expect(onSelect).toHaveBeenLastCalledWith('0')
    sessions = [sessions[0]]
    await render()
    expect(input().hasAttribute('aria-activedescendant')).toBe(false)
    await key('Enter')
    expect(onSelect).toHaveBeenLastCalledWith('1')
  })

  it('traps focus and closes only with Escape, the close control or the backdrop', async () => {
    await render()
    const close = container.querySelector<HTMLButtonElement>('button')!
    await key('Tab') // Browser handles ordinary Tab; the trap handles wraparound.
    close.focus()
    await act(async () => { close.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true })) })
    expect(document.activeElement).toBe(input())
    await act(async () => { input().dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true })) })
    expect(document.activeElement).toBe(close)
    await act(async () => container.querySelector<HTMLElement>('[role="dialog"]')!.click())
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => close.click())
    await act(async () => container.querySelector<HTMLElement>('.session-search-overlay')!.click())
    await key('Escape')
    expect(onClose).toHaveBeenCalledTimes(3)
  })

  it('distinguishes loading from an empty catalog', async () => {
    sessions = []
    await render(true)
    expect(container.textContent).toContain('正在加载对话')
    await render()
    expect(container.textContent).toContain('还没有最近对话')
  })
})
