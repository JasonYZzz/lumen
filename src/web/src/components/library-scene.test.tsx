// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { LibraryScene } from './library-scene'
import type { AssistantResource } from '@/lib/api/types'

Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT:true})
let root: Root
let host: HTMLDivElement
const resources: AssistantResource[] = Array.from({length:11},(_,i)=>({
  id:`resource-${i}`,title:`资料 ${i}`,kind:'note',revision:1,status:'active',saved:false,published:true,
  created_at:'2026-10-26T00:00:00Z',artifact_ref:`sha256:${'a'.repeat(64)}`,
}))

beforeEach(() => { host=document.createElement('div');document.body.append(host);root=createRoot(host) })
afterEach(async () => { await act(async()=>root.unmount());host.remove() })

it('bounds each shelf to ten real resources and opens only messages from its own frame and catalogue', async () => {
  const open=vi.fn()
  await act(async()=>root.render(<LibraryScene resources={resources} onOpen={open} />))
  const frame=host.querySelector('iframe')!
  expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
  expect(frame.srcdoc).toContain('资料 9')
  expect(frame.srcdoc).not.toContain('资料 10')
  await act(async()=>window.dispatchEvent(new MessageEvent('message',{source:window,data:{type:'lumen:open-resource',id:'resource-0'}})))
  expect(open).not.toHaveBeenCalled()
  await act(async()=>window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:'lumen:open-resource',id:'unknown'}})))
  expect(open).not.toHaveBeenCalled()
  await act(async()=>window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:'lumen:open-resource',id:'resource-0'}})))
  expect(open).toHaveBeenCalledExactlyOnceWith('resource-0')
  await act(async()=>host.querySelector<HTMLButtonElement>('[aria-label="下一层资料"]')!.click())
  expect(frame.srcdoc).toContain('资料 10')
  expect(frame.srcdoc).not.toContain('"id":"resource-0"')
  expect(host.textContent).toContain('11–11 / 11')
})

it('reports scene readiness and loading failure without hiding the fallback guidance', async () => {
  await act(async()=>root.render(<LibraryScene resources={resources.slice(0,1)} onOpen={vi.fn()} />))
  const frame=host.querySelector('iframe')!
  expect(host.querySelector('[role="status"]')?.textContent).toContain('正在布置')
  await act(async()=>window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:'lumen:shelf-ready'}})))
  expect(host.querySelector('[role="status"]')).toBeNull()
  await act(async()=>window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:'lumen:shelf-error'}})))
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('切换到网格或列表')
  expect(host.querySelector('iframe')).toBeNull()
})
