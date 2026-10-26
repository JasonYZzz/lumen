// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ModelSettingsControl, type ModelSettingsView } from './model-settings-control'

let root: Root
let container: HTMLDivElement
const save = vi.fn()
const selectModel = vi.fn()
const options = ['provider_default', 'low', 'high', 'max'].map(value => ({value, label: value}))
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class {observe() {} disconnect() {}})
  vi.clearAllMocks()
  save.mockResolvedValue(undefined)
  selectModel.mockResolvedValue(undefined)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })
async function mount(value = 'low', defaultLevel: 'high' | null = 'high') {
  await act(async () => root.render(<Harness value={value} defaultLevel={defaultLevel} />))
}
function Harness({value, defaultLevel}: {value: string; defaultLevel: 'high' | null}) {
  const [view, setView] = useState<ModelSettingsView>('settings')
  return <ModelSettingsControl value={value} options={options} model="Configured model" models={['Configured model', 'Another model']}
    defaultLevel={defaultLevel} view={view} onViewChange={setView} disabled={false} reasoningDisabled={false}
    description="For the next turn" modelNotice="Used next turn" onChange={save} onModelChange={selectModel} />
}
async function move(value: string) {
  const slider = document.querySelector<HTMLInputElement>('input[type="range"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(slider, value)
    slider.dispatchEvent(new Event('input', {bubbles: true}))
  })
  return slider
}
it('previews dragging and submits once on release using actual discrete levels', async () => {
  await mount()
  const slider = await move('2')
  expect(document.querySelector('output')?.textContent).toBe('最高')
  expect(save).not.toHaveBeenCalled()
  await act(async () => {
    slider.dispatchEvent(new Event('pointerup', {bubbles: true}))
    slider.dispatchEvent(new Event('pointerup', {bubbles: true}))
  })
  expect(save).toHaveBeenCalledExactlyOnceWith('max')
})
it('commits keyboard changes and preserves the previous setting on failure', async () => {
  await mount()
  save.mockRejectedValueOnce(new Error('Settings unavailable'))
  const slider = await move('1')
  await act(async () => slider.dispatchEvent(new KeyboardEvent('keyup', {key: 'ArrowRight', bubbles: true})))
  expect(save).toHaveBeenCalledWith('high')
  expect(document.querySelector('[role="alert"]')?.textContent).toBe('Settings unavailable')
  expect(document.querySelector('output')?.textContent).toBe('低')
})
it('restores provider_default explicitly and never invents an unknown default level', async () => {
  await mount('high')
  const reset = document.querySelector<HTMLButtonElement>('[aria-label="恢复供应商默认"]')!
  await act(async () => reset.click())
  expect(save).toHaveBeenCalledWith('provider_default')
  await mount('provider_default', null)
  expect(document.querySelector('output')?.textContent).toBe('默认')
  expect(document.querySelector('input')?.getAttribute('aria-valuetext')).toBe('默认')
  expect(document.body.textContent).toContain('供应商未公开默认档位')
})
it('keeps effort settings independent of companion images', async () => {
  await mount('provider_default', 'high')
  expect(document.querySelector('output')?.textContent).toBe('默认 · 高')
  await move('0')
  expect(document.querySelector('output')?.textContent).toBe('低')
  expect(document.querySelector('[role="dialog"] img')).toBeNull()
  expect(document.body.textContent).not.toContain('成长')
})
it('uses one trigger and the same panel when choosing a model and returning to effort', async () => {
  await mount()
  expect(container.querySelectorAll('.model-settings-control > button')).toHaveLength(1)
  const panel = document.querySelector('[role="dialog"]')!
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.click())
  expect(document.querySelector('[role="dialog"]')).toBe(panel)
  expect(document.querySelector('[aria-label="搜索模型"]')).not.toBeNull()
  await act(async () => document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1].click())
  expect(selectModel).toHaveBeenCalledExactlyOnceWith('Another model')
  expect(document.querySelector('[role="dialog"]')).toBe(panel)
  expect(document.querySelector('[aria-label="思考强度档位"]')).not.toBeNull()
})
it('preserves the model search and displays errors when switching fails', async () => {
  await mount()
  selectModel.mockRejectedValueOnce(new Error('Model unavailable'))
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.click())
  await act(async () => document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1].click())
  expect(document.querySelector('[role="alert"]')?.textContent).toBe('Model unavailable')
  expect(document.querySelector('[aria-label="搜索模型"]')).not.toBeNull()
})
it('does not reopen a panel closed while a model switch is pending', async () => {
  await mount()
  let finish!: () => void
  selectModel.mockReturnValue(new Promise<void>(resolve => {finish = resolve}))
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.click())
  await act(async () => document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1].click())
  await act(async () => container.querySelector<HTMLButtonElement>('button')!.click())
  await act(async () => finish())
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})
it('keeps model selection available but hides old effort choices for a queued model', async () => {
  await act(async () => root.render(<ModelSettingsControl value="high" options={options} model="Current model"
    models={['Current model', 'Queued model']} pendingModel="Queued model" defaultLevel="high" view="settings"
    onViewChange={vi.fn()} disabled={false} reasoningDisabled description="Old model levels"
    modelNotice="本轮继续使用 Current model；结束后切换到 Queued model。" onChange={save} onModelChange={selectModel} />))
  expect(container.textContent).toContain('Queued model')
  expect(container.textContent).toContain('待切换')
  expect(document.querySelector('output')?.textContent).toBe('等待切换')
  expect(document.querySelector('[aria-label="思考强度档位"]')).toBeNull()
  expect(document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.disabled).toBe(false)
})
it('shows the real model ID while preserving alias selection', async () => {
  await act(async () => root.render(<ModelSettingsControl value="high" options={options} model="kimi-k3"
    modelId="openai:k3.1-pro" models={['kimi-k3', 'Another model']} defaultLevel="high" view="models"
    onViewChange={vi.fn()} disabled={false} reasoningDisabled={false} description="For the next turn"
    modelNotice="Used next turn" onChange={save} onModelChange={selectModel} />))
  expect(container.querySelector('.choice-menu-value')?.textContent).toBe('k3.1-pro')
  const selected = document.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"]')!
  expect(selected.querySelector('strong')?.textContent).toBe('k3.1-pro')
  expect(selected.querySelector('small')?.textContent).toBe('配置：kimi-k3')
  await act(async () => selected.click())
  expect(selectModel).not.toHaveBeenCalled()
})
it('shows and searches configured IDs while switching with their aliases', async () => {
  await act(async () => root.render(<ModelSettingsControl value="high" options={options} model="Current model"
    modelIds={{'kimi-k3': 'openai:k3.1-pro'}} models={['Current model', 'kimi-k3']} defaultLevel="high" view="models"
    pendingModel="kimi-k3" onViewChange={vi.fn()} disabled={false} reasoningDisabled description="For the next turn"
    modelNotice="Used next turn" onChange={save} onModelChange={selectModel} />))
  expect(container.querySelector('.choice-menu-value')?.textContent).toBe('k3.1-pro')
  const search = document.querySelector<HTMLInputElement>('[aria-label="搜索模型"]')!
  for (const query of ['k3.1-pro', 'kimi-k3']) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, query)
      search.dispatchEvent(new Event('input', {bubbles: true}))
    })
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(1)
    expect(document.querySelector('[role="option"] strong')?.textContent).toBe('k3.1-pro')
  }
})
it('closes model search when Tab leaves its last sequential focus stop', async () => {
  await mount()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.click())
  const search = document.querySelector<HTMLInputElement>('[aria-label="搜索模型"]')!
  search.focus()
  const event = new KeyboardEvent('keydown', {key: 'Tab', bubbles: true, cancelable: true})
  await act(async () => search.dispatchEvent(event))
  expect(event.defaultPrevented).toBe(true)
  expect(document.querySelector('[role="dialog"]')).toBeNull()
  expect(document.activeElement).toBe(container.querySelector('button'))
})
it('returns arrow-focused options to search on Shift+Tab and closes on Tab', async () => {
  await mount()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.click())
  const option = document.querySelector<HTMLButtonElement>('[role="option"]')!
  option.focus()
  await act(async () => option.dispatchEvent(new KeyboardEvent('keydown', {key: 'Tab', shiftKey: true, bubbles: true, cancelable: true})))
  expect(document.activeElement).toBe(document.querySelector('[aria-label="搜索模型"]'))
  option.focus()
  await act(async () => option.dispatchEvent(new KeyboardEvent('keydown', {key: 'Tab', bubbles: true, cancelable: true})))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})
it('keeps search open when Escape cancels IME composition', async () => {
  await mount()
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="切换模型"]')!.click())
  const search = document.querySelector<HTMLInputElement>('[aria-label="搜索模型"]')!
  for (const composition of [{isComposing: true}, {keyCode: 229}]) {
    await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, ...composition})))
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
  }
  await act(async () => search.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})))
  expect(document.querySelector('[role="dialog"]')).toBeNull()
})
