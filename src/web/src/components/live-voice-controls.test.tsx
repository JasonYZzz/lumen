// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { LiveVoiceControls } from './live-voice-controls'
const media = vi.hoisted(() => ({ clients: [] as Array<{ callbacks: Record<string, (...args: never[]) => void>; stop: ReturnType<typeof vi.fn>; setVisualizationActive: ReturnType<typeof vi.fn>; setMuted: ReturnType<typeof vi.fn> }> }))
vi.mock('@/lib/live/realtime-client', () => ({
  silentAudioLevels: { input: 0, output: 0, outputAvailable: false },
  RealtimeVoiceClient: class {
    callbacks = {}; stop = vi.fn().mockResolvedValue(undefined); setVisualizationActive = vi.fn(); setMuted = vi.fn()
    constructor() { media.clients.push(this) }
    async connect(_id: string, callbacks: Record<string, (...args: never[]) => void>) { this.callbacks = callbacks; return 'live' }
    async devices() { return [] }
  },
}))
vi.mock('@/lib/api/client', () => ({ lumenApi: { decideLiveApproval: vi.fn().mockResolvedValue(undefined) } }))
let container: HTMLDivElement, root: Root
beforeEach(() => {
  media.clients.length = 0; vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })
async function click(label: string) {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!
  expect(button).toBeTruthy(); await act(async () => button.click())
}
it('expands existing media, retains approval and mute/end controls with reduced motion, and restores focus', async () => {
  const ensure = vi.fn().mockResolvedValue('session')
  await act(async () => root.render(<LiveVoiceControls enabled blocked={false} ensureSession={ensure} />))
  await click('开始实时语音'); await click('展开语音视图')
  expect(media.clients).toHaveLength(1); expect(ensure).toHaveBeenCalledTimes(1)
  const dialog = document.querySelector('[role="dialog"]')!
  expect(dialog.querySelector('canvas')).toBeNull(); expect(dialog.textContent).toContain('收起视图会继续')
  await click('麦克风静音'); expect(media.clients[0].setMuted).toHaveBeenLastCalledWith(true)
  await act(async () => { media.clients[0].callbacks.onEvent({ type: 'live.tool.approval_pending', data: { call_id: 'call', name: 'write_file', risk: 'confirm', arguments: { path: 'x' } } } as never) })
  expect(dialog.textContent).toContain('write_file')
  expect(dialog.textContent).toContain('允许一次')
  await click('收起语音视图')
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(media.clients[0].stop).not.toHaveBeenCalled()
  expect(document.activeElement?.getAttribute('aria-label')).toBe('展开语音视图')
  await click('结束实时语音'); expect(media.clients[0].stop).toHaveBeenCalledTimes(1)
  expect(document.querySelector('button[aria-label="开始实时语音"]')).toBeTruthy()
})
it('does not connect after a pending start is cancelled', async () => {
  let deliver!: (id: string) => void
  const ensure = () => new Promise<string>(resolve => { deliver = resolve })
  await act(async () => root.render(<LiveVoiceControls enabled blocked={false} ensureSession={ensure} />))
  await click('开始实时语音'); await click('结束实时语音')
  await act(async () => deliver('late-session'))
  expect(media.clients).toHaveLength(0)
})
