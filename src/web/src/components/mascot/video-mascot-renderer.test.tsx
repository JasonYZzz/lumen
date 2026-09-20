// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { VideoMascotRenderer } from './video-mascot-renderer'
import type { MascotActivity, MascotMotionMode } from './mascot-types'

let container: HTMLDivElement, root: Root
let videos: HTMLVideoElement[]
let context: MockInstance<HTMLCanvasElement['getContext']>
let play: MockInstance<HTMLMediaElement['play']>, pause: MockInstance<HTMLMediaElement['pause']>
let frames: Map<number, FrameRequestCallback>, frameId: number, now: number
let glCalls: Map<PropertyKey, ReturnType<typeof vi.fn>>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  frames = new Map(); frameId = 0; now = 0
  vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => {
    frames.set(++frameId, callback); return frameId
  }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id: number) => frames.delete(id)))
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  vi.stubGlobal('requestIdleCallback', undefined)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  videos = []
  const create = document.createElement.bind(document)
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string, options?: ElementCreationOptions) => {
    const element = create(tag, options)
    if (tag === 'video') videos.push(element as HTMLVideoElement)
    return element
  }) as typeof document.createElement)
  glCalls = new Map()
  const gl = new Proxy({}, { get: (_target, key) => {
    if (typeof key === 'string' && key === key.toUpperCase()) return 0
    if (!glCalls.has(key)) glCalls.set(key, vi.fn(() => key === 'getShaderParameter' || key === 'getProgramParameter' ? true : {}))
    return glCalls.get(key)
  } })
  context = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(gl as WebGLRenderingContext)
  play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
  pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
})
afterEach(async () => {
  await act(async () => root.unmount())
  container.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})
const render = async (mode: MascotMotionMode = 'full', active = true, interaction = 0, activity: MascotActivity = 'idle', paused = false) => {
  await act(async () => root.render(<VideoMascotRenderer activity={activity} motionMode={mode} active={active} interaction={interaction} paused={paused} />))
}
const decode = async () => {
  Object.defineProperties(videos[0], {
    readyState: { value: 2 }, videoWidth: { value: 1024 }, videoHeight: { value: 512 },
  })
  await act(async () => videos[0].dispatchEvent(new Event('loadeddata')))
}
const tick = async (time: number) => {
  now = time
  const callbacks = [...frames.values()]; frames.clear()
  await act(async () => callbacks.forEach(callback => callback(time)))
}

describe('video mascot lifecycle', () => {
  it.each(['static', 'reduced'] as const)('uses only a poster in %s mode', async mode => {
    await render(mode)
    expect(videos).toHaveLength(0)
    expect(context).not.toHaveBeenCalled()
    expect(container.querySelector('img')?.hidden).toBe(false)
  })
  it('fails closed to a poster if WebGL is unavailable', async () => {
    context.mockReturnValue(null)
    await render()
    expect(container.querySelector('[data-renderer="static-fallback"]')).not.toBeNull()
    expect(videos).toHaveLength(0)
  })
  it('loads neither clip offscreen, then loads idle before reaction', async () => {
    await render('full', false)
    expect(videos.every(video => !video.hasAttribute('src'))).toBe(true)
    expect(play).not.toHaveBeenCalled()
    await render()
    expect(videos[0].getAttribute('src')).toContain('fox-idle')
    expect(videos[1].hasAttribute('src')).toBe(false)
    expect(container.querySelector('img')?.hidden).toBe(false)
  })
  it('prefetches reaction only after a decoded idle frame', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await render()
    await act(async () => vi.advanceTimersByTime(1000))
    expect(videos[1].hasAttribute('src')).toBe(false)
    await decode()
    expect(container.querySelector('img')?.hidden).toBe(true)
    expect(videos[1].hasAttribute('src')).toBe(false)
    await act(async () => vi.advanceTimersByTime(300))
    expect(videos[1].getAttribute('src')).toContain('fox-react')
    expect(context).toHaveBeenCalledTimes(1)
  })
  it.each(['offscreen', 'paused'] as const)('cancels pending prefetch while %s', async state => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await render(); await decode()
    await render('full', state !== 'offscreen', 0, 'idle', state === 'paused')
    await act(async () => vi.advanceTimersByTime(1000))
    expect(videos[1].hasAttribute('src')).toBe(false)
    expect(frames.size).toBe(0)
    await render(); await tick(0)
    await act(async () => vi.advanceTimersByTime(300))
    expect(videos[1].hasAttribute('src')).toBe(true)
  })
  it('loads reaction immediately when the user focuses the composer', async () => {
    await render()
    videos[0].currentTime = 3
    await render('full', true, 0, 'focused')
    expect(videos[1].getAttribute('src')).toContain('fox-react')
    expect(play.mock.instances.at(-1)).toBe(videos[0])
  })
  it('limits fallback texture uploads to 30 fps and caches uniform locations', async () => {
    await render(); await decode(); await tick(0)
    const uploads = glCalls.get('texImage2D')!
    uploads.mockClear()
    const lookups = glCalls.get('getUniformLocation')!.mock.calls.length
    await tick(8); await tick(16); await tick(24)
    expect(uploads).not.toHaveBeenCalled()
    await tick(40); await tick(80)
    expect(uploads).toHaveBeenCalledTimes(2)
    expect(glCalls.get('getUniformLocation')).toHaveBeenCalledTimes(lookups)
  })
  it('uses decoded video callbacks when available and cancels them offscreen', async () => {
    await render()
    const request = vi.fn(() => 99), cancel = vi.fn()
    videos[0].requestVideoFrameCallback = request
    videos[0].cancelVideoFrameCallback = cancel
    await render('full', false)
    await render()
    expect(request).toHaveBeenCalledTimes(1)
    expect(frames.size).toBe(0)
    await render('full', false)
    expect(cancel).toHaveBeenCalledWith(99)
  })
  it('returns to the poster on context loss and releases GPU resources on unmount', async () => {
    await render(); await decode()
    const event = new Event('webglcontextlost', { cancelable: true })
    await act(async () => container.querySelector('canvas')!.dispatchEvent(event))
    expect(event.defaultPrevented).toBe(true)
    expect(container.querySelector('[data-renderer="static-fallback"]')).not.toBeNull()
    expect(container.querySelector('img')?.hidden).toBe(false)
    expect(frames.size).toBe(0)
    await render('static')
    expect(glCalls.get('deleteTexture')).toHaveBeenCalledTimes(1)
    expect(glCalls.get('deleteProgram')).toHaveBeenCalledTimes(1)
    expect(videos.every(video => !video.hasAttribute('src'))).toBe(true)
  })
  it('pauses offscreen and resumes without rewinding', async () => {
    await render()
    expect(videos).toHaveLength(2)
    expect(videos.every(video => video.muted && video.playsInline)).toBe(true)
    videos[0].currentTime = 3
    await render('full', false)
    expect(pause).toHaveBeenCalled()
    const plays = play.mock.calls.length
    await render('full', false, 1)
    expect(play).toHaveBeenCalledTimes(plays)
    await render()
    expect(videos[0].currentTime).toBe(3)
    expect(play.mock.calls.length).toBeGreaterThan(plays)
  })
  it('queues a click and switches only at the authored endpoint', async () => {
    await render()
    videos[0].currentTime = 3
    await render('full', true, 1)
    expect(videos[0].currentTime).toBe(3)
    await act(async () => videos[0].dispatchEvent(new Event('ended')))
    expect(play.mock.instances.at(-1)).toBe(videos[1])
    await act(async () => videos[1].dispatchEvent(new Event('ended')))
    expect(play.mock.instances.at(-1)).toBe(videos[0])
  })
  it('releases decoder sources when switching to static mode', async () => {
    await render()
    await render('static')
    expect(videos.every(video => !video.hasAttribute('src'))).toBe(true)
    expect(container.querySelector('img')?.hidden).toBe(false)
  })
})
