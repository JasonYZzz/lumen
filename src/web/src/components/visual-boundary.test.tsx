// @vitest-environment jsdom
import { act, lazy, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { VisualBoundary } from './visual-boundary'
it('contains an actual rejected lazy import and keeps sibling controls', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
  const container = document.createElement('div'); document.body.append(container); const root = createRoot(container)
  const failure = vi.fn(), reject = lazy(() => Promise.reject(new Error('ChunkLoadError')))
  const render = () => {
    const Scene = reject
    return <><button type="button">结束通话</button><a href="blob:original" download>下载</a>
      <VisualBoundary onFailure={failure} fallback={<p>静态回退</p>}><Suspense fallback="加载中"><Scene /></Suspense></VisualBoundary></>
  }
  try {
    await act(async () => root.render(render()))
    expect(container.textContent).toContain('静态回退'); expect(failure).toHaveBeenCalledTimes(1)
    expect(container.querySelector('button')?.textContent).toBe('结束通话'); expect(container.querySelector('a[download]')).toBeTruthy()
  } finally { await act(async () => root.unmount()); container.remove(); consoleError.mockRestore(); vi.unstubAllGlobals() }
})
