// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { VideoMascotRenderer } from './video-mascot-renderer'
import type { MascotMotionMode } from './mascot-types'

let container: HTMLDivElement, root: Root
let videos: HTMLVideoElement[]
let context: MockInstance<HTMLCanvasElement['getContext']>
let play: MockInstance<HTMLMediaElement['play']>, pause: MockInstance<HTMLMediaElement['pause']>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  videos = []
  const create = document.createElement.bind(document)
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string, options?: ElementCreationOptions) => {
    const element = create(tag, options)
    if (tag === 'video') videos.push(element as HTMLVideoElement)
    return element
  }) as typeof document.createElement)
  const gl = new Proxy({}, { get: (_target, key) => key === 'getShaderParameter' || key === 'getProgramParameter'
    ? () => true : typeof key === 'string' && key === key.toUpperCase() ? 0 : vi.fn(() => ({})) })
  context = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(gl as WebGLRenderingContext)
  play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
  pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
})
afterEach(async () => {
  await act(async () => root.unmount())
  container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})
const render = async (mode: MascotMotionMode = 'full', active = true, interaction = 0) => {
  await act(async () => root.render(<VideoMascotRenderer activity="idle" motionMode={mode} active={active} interaction={interaction} />))
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
