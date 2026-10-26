// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AppearanceControl, useAppearance } from './appearance-control'

let root: Root
let container: HTMLDivElement
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  localStorage.clear()
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => {
  await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})
function Harness() {
  const appearance = useAppearance()
  return <div data-stage={appearance.stage}><AppearanceControl stage={appearance.stage} onChange={appearance.selectStage}
    note={appearance.storageAvailable ? 'saved' : 'not saved'} /></div>
}
async function slide(value: number) {
  await act(async () => {
    const range = container.querySelector('input')!
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(range, String(value))
    range.dispatchEvent(new Event('input', {bubbles: true}))
  })
}
it('restores a saved color and form without overwriting it during hydration', async () => {
  localStorage.setItem('lumen.appearance', 'blue')
  await act(async () => root.render(<Harness />))
  expect(document.documentElement.dataset.appearance).toBe('blue')
  expect(container.querySelector('input')?.value).toBe('3')
  expect(container.querySelector('[data-stage]')?.getAttribute('data-stage')).toBe('3')
  expect(localStorage.getItem('lumen.appearance')).toBe('blue')
})
it('updates discrete slider choices immediately and synchronizes other tabs', async () => {
  await act(async () => root.render(<Harness />))
  const range = container.querySelector('input')!
  await slide(4)
  expect(document.documentElement.dataset.appearance).toBe('lilac')
  expect(range.getAttribute('aria-valuetext')).toContain('5 / 5')
  expect(localStorage.getItem('lumen.appearance')).toBe('lilac')
  await slide(2)
  expect(localStorage.getItem('lumen.appearance')).toBe('mint')
  await act(async () => window.dispatchEvent(new StorageEvent('storage', {key: 'lumen.appearance', newValue: 'apricot'})))
  expect(document.documentElement.dataset.appearance).toBe('apricot')
  expect(range.value).toBe('1')
  await act(async () => window.dispatchEvent(new StorageEvent('storage', {key: null, newValue: null})))
  expect(document.documentElement.dataset.appearance).toBe('pink')
})
it('keeps the control usable with unavailable browser storage and cleans up its global theme', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {throw new Error('disabled')})
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {throw new Error('disabled')})
  await act(async () => root.render(<Harness />))
  await slide(4)
  expect(document.documentElement.dataset.appearance).toBe('lilac')
  expect(container.textContent).toContain('not saved')
  await act(async () => root.unmount())
  expect(document.documentElement.dataset.appearance).toBeUndefined()
  root = createRoot(container)
})
