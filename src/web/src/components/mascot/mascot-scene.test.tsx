// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MascotScene } from './mascot-scene'

let container: HTMLDivElement
let root: ReturnType<typeof createRoot>
let imageRequests: HTMLImageElement[]
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('matchMedia', () => ({matches: false, addEventListener() {}, removeEventListener() {}}))
  imageRequests = []
  vi.stubGlobal('Image', function () {
    const image = document.createElement('img'); imageRequests.push(image); return image
  })
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => {
  await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.useRealTimers()
  Reflect.deleteProperty(document, 'hidden')
})
it('changes the image with the stage, and falls back to the base image on an asset error', async () => {
  vi.useFakeTimers()
  await act(async () => root.render(<MascotScene activity="idle" stage={4} />))
  expect(container.querySelector('img')?.src).toContain('fox-stage-05-v2-480.webp')
  expect(container.querySelector('img')?.srcset).toContain('fox-stage-05-v2-960.webp 960w')
  await act(async () => container.querySelector('img')!.dispatchEvent(new Event('error')))
  expect(container.querySelector('img')?.src).toContain('fox-stage-01-v2-480.webp')
  await act(async () => root.render(<MascotScene activity="focused" stage={2} />))
  await act(async () => imageRequests[0].dispatchEvent(new Event('load')))
  await act(async () => vi.advanceTimersByTime(320))
  expect(container.querySelector('img')?.src).toContain('fox-stage-03-v2-480.webp')
})
it('loads before crossfading, discards stale loads, and clears the outgoing image and timer', async () => {
  vi.useFakeTimers()
  await act(async () => root.render(<MascotScene activity="idle" stage={1} />))
  await act(async () => root.render(<MascotScene activity="idle" stage={2} />))
  expect(container.querySelector('img')?.src).toContain('fox-stage-02-v2-480.webp')
  expect(container.querySelector('img')?.srcset).toContain('fox-stage-02-v2-960.webp 960w')
  await act(async () => root.render(<MascotScene activity="idle" stage={4} />))
  await act(async () => imageRequests[0].dispatchEvent(new Event('load')))
  expect(container.querySelector('img')?.src).toContain('fox-stage-02-v2-480.webp')
  await act(async () => imageRequests[1].dispatchEvent(new Event('load')))
  expect(Array.from(container.querySelectorAll('img')).map(image => image.src))
    .toEqual([expect.stringContaining('fox-stage-02-v2-480.webp'), expect.stringContaining('fox-stage-05-v2-480.webp')])
  await act(async () => vi.advanceTimersByTime(320))
  expect(container.querySelectorAll('img')).toHaveLength(1)
  expect(container.querySelector('img')?.src).toContain('fox-stage-05-v2-480.webp')
  await act(async () => root.render(<MascotScene activity="idle" stage={3} />))
  await act(async () => imageRequests[2].dispatchEvent(new Event('load')))
  await act(async () => root.unmount())
  expect(vi.getTimerCount()).toBe(0)
  root = createRoot(container)
})
it('retains the current image when the requested asset fails and switches without a fade in static mode', async () => {
  await act(async () => root.render(<MascotScene activity="idle" stage={1} motionMode="static" />))
  await act(async () => root.render(<MascotScene activity="idle" stage={3} motionMode="static" />))
  await act(async () => imageRequests[0].dispatchEvent(new Event('error')))
  expect(container.querySelector('img')?.src).toContain('fox-stage-02-v2-480.webp')
  expect(imageRequests).toHaveLength(1)
  await act(async () => root.render(<MascotScene activity="idle" stage={4} motionMode="static" />))
  await act(async () => imageRequests[1].dispatchEvent(new Event('load')))
  expect(container.querySelectorAll('img')).toHaveLength(1)
  expect(container.querySelector('img')?.src).toContain('fox-stage-05-v2-480.webp')
})
it.each(['static', 'reduced', 'hidden'] as const)('ends an active crossfade immediately when motion becomes %s', async mode => {
  vi.useFakeTimers()
  await act(async () => root.render(<MascotScene activity="idle" stage={1} />))
  await act(async () => root.render(<MascotScene activity="idle" stage={3} />))
  await act(async () => imageRequests[0].dispatchEvent(new Event('load')))
  expect(container.querySelectorAll('img')).toHaveLength(2)
  await act(async () => {
    if (mode === 'hidden') {
      Object.defineProperty(document, 'hidden', {configurable: true, value: true})
      document.dispatchEvent(new Event('visibilitychange'))
    } else root.render(<MascotScene activity="idle" stage={3} motionMode={mode} />)
  })
  expect(container.querySelectorAll('img')).toHaveLength(1)
  expect(container.querySelector('img')?.src).toContain('fox-stage-04-v2-480.webp')
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => {
    Object.defineProperty(document, 'hidden', {configurable: true, value: false})
    document.dispatchEvent(new Event('visibilitychange'))
    root.render(<MascotScene activity="idle" stage={3} motionMode="full" />)
  })
  expect(container.querySelectorAll('img')).toHaveLength(1)
})
it('keeps greetings usable in static mode and clears the timer on unmount', async () => {
  vi.useFakeTimers()
  await act(async () => root.render(<MascotScene activity="idle" motionMode="static" />))
  expect(container.querySelectorAll('img')).toHaveLength(1)
  await act(async () => container.querySelector('button')!.click())
  expect(container.querySelector('[role="status"]')?.textContent).toContain('打了个招呼')
  await act(async () => vi.advanceTimersByTime(700))
  expect(container.querySelector('[role="status"]')?.textContent).toBe('')
  await act(async () => container.querySelector('button')!.click())
  await act(async () => root.unmount())
  expect(vi.getTimerCount()).toBe(0)
  root = createRoot(container)
})
it('only loads the blink frame while the base fox is visible with full motion', async () => {
  await act(async () => root.render(<MascotScene activity="idle" />))
  expect(container.querySelectorAll('img')).toHaveLength(2)
  const blink = container.querySelectorAll('img')[1]
  expect(blink.src).toContain('v2-blink.webp')
  await act(async () => blink.dispatchEvent(new Event('load')))
  expect(blink.dataset.ready).toBe('true')
  await act(async () => {
    Object.defineProperty(document, 'hidden', {configurable: true, value: true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  expect(container.querySelectorAll('img')).toHaveLength(1)
  await act(async () => {
    Object.defineProperty(document, 'hidden', {configurable: true, value: false})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  expect(container.querySelectorAll('img')).toHaveLength(2)
  await act(async () => root.render(<MascotScene activity="idle" motionMode="reduced" />))
  expect(container.querySelectorAll('img')).toHaveLength(1)
})
it('bounds pointer feedback and resets it on leave without updating React state', async () => {
  await act(async () => root.render(<MascotScene activity="idle" />))
  const button = container.querySelector('button')!
  vi.spyOn(button, 'getBoundingClientRect').mockReturnValue({left: 0, width: 280} as DOMRect)
  await act(async () => button.dispatchEvent(new MouseEvent('pointermove', {clientX: 280, bubbles: true})))
  expect(button.style.getPropertyValue('--fox-lean')).toBe('2.5deg')
  await act(async () => button.dispatchEvent(new MouseEvent('pointerout', {bubbles: true})))
  expect(button.style.getPropertyValue('--fox-lean')).toBe('')
})
