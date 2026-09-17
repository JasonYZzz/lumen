// @vitest-environment jsdom
import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ReportReadingButton } from './markdown-report'

let root: Root
let container: HTMLDivElement
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })
const content = '# 概览\n[官方资料](https://example.com/)\n## 细节\n说明\n## 细节\n更多说明'

describe('report reading and sources', () => {
  it('navigates unique headings and closes only the top modal, then returns focus', async () => {
    await act(async () => root.render(<StrictMode><ReportReadingButton content={content} entries={[]} /></StrictMode>))
    const trigger = container.querySelector<HTMLButtonElement>('button')!
    trigger.focus()
    await act(async () => trigger.click())
    const report = document.querySelector<HTMLElement>('[aria-label="完整回答"]')!
    const headings = report.querySelectorAll<HTMLElement>('h1,h2')
    expect(new Set(Array.from(headings, (heading) => heading.id)).size).toBe(3)
    const scroll = vi.fn(); headings[2].scrollIntoView = scroll
    await act(async () => report.querySelectorAll<HTMLButtonElement>('.report-contents button')[2].click())
    expect(scroll).toHaveBeenCalled()
    expect(document.activeElement).toBe(headings[2])
    const link = report.querySelector<HTMLAnchorElement>('.web-source-link')!
    link.focus()
    await act(async () => link.click())
    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(2)
    expect(document.querySelector('.source-detail')?.textContent).toContain('尚无可展示的检索摘要')
    expect(document.querySelector('.source-detail')?.textContent).toContain('正文链接')
    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(document.activeElement).toBe(link)
    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(0)
    expect(document.activeElement).toBe(trigger)
  })
  it('preserves modifier-click navigation to the original URL', async () => {
    await act(async () => root.render(<ReportReadingButton content={content} entries={[]} />))
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click())
    const link = document.querySelector<HTMLAnchorElement>('.web-source-link')!
    const click = new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true })
    await act(async () => link.dispatchEvent(click))
    expect(click.defaultPrevented).toBe(false)
    expect(document.querySelector('.sources-panel')).toBeNull()
  })
})
