// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SceneCanvas } from './scene-canvas'
const renderer = vi.hoisted(() => ({ invalidate: vi.fn(), gl: { domElement: null as unknown as HTMLCanvasElement }, props: {} as Record<string, unknown> }))
vi.mock('@react-three/fiber', () => ({
  Canvas: ({ children, ...props }: { children: React.ReactNode }) => { renderer.props = props; return <div>{children}</div> },
  useThree: () => renderer,
}))
let root: Root, container: HTMLDivElement
beforeEach(() => {
  vi.useFakeTimers(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false }))); renderer.invalidate.mockClear()
  renderer.gl.domElement = document.createElement('canvas')
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.useRealTimers(); vi.unstubAllGlobals() })
it('pauses the demand loop in background, resumes once, and removes context-loss listeners on close', async () => {
  const failure = vi.fn()
  const render = (active: boolean) => act(async () => root.render(<SceneCanvas active={active} animate onFailure={failure} fallback={<p>静态</p>}>{null}</SceneCanvas>))
  await render(true); await act(async () => vi.advanceTimersByTime(100))
  expect(renderer.invalidate.mock.calls.length).toBeGreaterThan(1)
  expect(renderer.props.frameloop).toBe('demand')
  await render(false); const calls = renderer.invalidate.mock.calls.length
  await act(async () => vi.advanceTimersByTime(1000)); expect(renderer.invalidate).toHaveBeenCalledTimes(calls)
  expect(renderer.props.frameloop).toBe('never')
  await render(true); expect(renderer.invalidate).toHaveBeenCalledTimes(calls + 1)
  const lost = new Event('webglcontextlost', { cancelable: true })
  await act(async () => renderer.gl.domElement.dispatchEvent(lost))
  expect(lost.defaultPrevented).toBe(true); expect(failure).toHaveBeenCalledTimes(1)
  await act(async () => root.render(null)); expect(vi.getTimerCount()).toBe(0)
  renderer.gl.domElement.dispatchEvent(new Event('webglcontextlost')); expect(failure).toHaveBeenCalledTimes(1)
})
