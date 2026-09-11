// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { lumenApi } from '../lib/api/client'
import type { AttachmentRef } from '../lib/api/types'
import { AttachmentImage } from './attachment-image'
import { Composer } from './composer'
import { ConversationTurn } from './lumen-app'
import { initialRunState, runReducer } from '../lib/state/run-reducer'
import type { EventEnvelope } from '../lib/api/types'

vi.mock('../lib/api/client', () => ({ lumenApi: { readAttachment: vi.fn(), uploadAttachment: vi.fn() } }))
const attachment: AttachmentRef = {
  artifactRef: `sha256:${'a'.repeat(64)}`, kind: 'image', mediaType: 'image/png', filename: 'example.png', byteSize: 12,
}
let root: Root
let container: HTMLDivElement
beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  URL.createObjectURL = vi.fn(() => 'blob:preview')
  URL.revokeObjectURL = vi.fn()
  vi.mocked(lumenApi.readAttachment).mockResolvedValue(new Blob())
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })

it('reloads a stored image, enlarges it, closes with Escape and releases its URL', async () => {
  await act(async () => root.render(<AttachmentImage attachment={attachment} />))
  expect(container.querySelector('img')?.src).toBe('blob:preview')
  await act(async () => container.querySelector('button')!.click())
  expect(document.querySelector('[role="dialog"] img')?.getAttribute('alt')).toBe('example.png')
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  await act(async () => root.render(null))
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview')
})

it('offers retry after a failed historical image load', async () => {
  vi.mocked(lumenApi.readAttachment).mockRejectedValueOnce(new Error('missing'))
  await act(async () => root.render(<AttachmentImage attachment={attachment} />))
  expect(container.textContent).toContain('加载失败')
  await act(async () => container.querySelector('button')!.click())
  expect(container.querySelector('img')).not.toBeNull()
})

it('renders an SSE attachment inside the user bubble and displays deltas before completion', async () => {
  let state = initialRunState
  const send = async (type: EventEnvelope['type'], data: Record<string, unknown>) => {
    state = runReducer(state, { type: 'event', event: {
      version: 1, sequence: state.timeline.length + 1, sessionId: 's', runId: 'r',
      createdAt: '', type, data,
    } })
    await act(async () => root.render(<ConversationTurn active={state.status === 'running'}
      turn={{ id: 'r', user: state.timeline[0], response: state.timeline.slice(1) }} onApproval={vi.fn()} />))
  }
  await send('run.started', { prompt: '描述图片', attachments: [{
    artifact_ref: attachment.artifactRef, kind: 'image', media_type: 'image/png',
    filename: attachment.filename, byte_size: attachment.byteSize,
  }] })
  expect(container.querySelector('.timeline-user img')?.getAttribute('alt')).toBe('example.png')
  await send('assistant.delta', { text: '第一段描述' })
  expect(state.status).toBe('running')
  expect(container.querySelector('.timeline-assistant')?.textContent).toContain('第一段描述')
  await send('assistant.delta', { text: '，第二段细节' })
  expect(container.querySelector('.timeline-assistant')?.textContent).toContain('第一段描述，第二段细节')
  await send('run.completed', {})
  expect(container.querySelector('.timeline-user img')).not.toBeNull()
})

it('previews uploads immediately, blocks sending, retains partial success and allows removal', async () => {
  let finish!: (value: AttachmentRef) => void
  vi.mocked(lumenApi.uploadAttachment)
    .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    .mockRejectedValueOnce(new Error('upload failed'))
  function Draft() {
    const [attachments, setAttachments] = useState<AttachmentRef[]>([])
    return <Composer value="look" busy={false} queueMode="steer" slashCommands={[]}
      attachments={attachments} imageInputEnabled onChange={vi.fn()} onQueueModeChange={vi.fn()}
      onAttachmentsChange={setAttachments} onSubmit={vi.fn()} onStop={vi.fn()} />
  }
  await act(async () => root.render(<Draft />))
  const input = container.querySelector('input[type="file"]')!
  Object.defineProperty(input, 'files', { value: [
    new File(['image'], 'example.png', { type: 'image/png' }),
    new File(['image'], 'failed.png', { type: 'image/png' }),
  ] })
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })))
  expect(container.querySelectorAll('img')).toHaveLength(2)
  expect(container.querySelector<HTMLButtonElement>('.send-button')?.disabled).toBe(true)
  await act(async () => finish(attachment))
  expect(container.textContent).toContain('上传失败：failed.png')
  expect(container.querySelectorAll('img')).toHaveLength(1)
  expect(container.querySelector<HTMLButtonElement>('.send-button')?.disabled).toBe(false)
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="移除 example.png"]')!.click())
  expect(container.querySelector('img')).toBeNull()
})
