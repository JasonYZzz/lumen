// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { providers } from '../../desktop/bootstrap/model-presets.js'

let invoke
let eventListener
beforeEach(async () => {
  vi.resetModules()
  document.body.innerHTML = '<main></main>'
  window.scrollTo = vi.fn()
  invoke = vi.fn(async command => command === 'bootstrap'
    ? { pending: { type: 'configuration_required', request_id: 'first' } } : undefined)
  window.__TAURI__ = { core: { invoke }, event: { listen: async (_, listener) => { eventListener = listener } } }
  await import('../../desktop/bootstrap/app.js')
})

it.each(providers.flatMap((provider, index) => provider.models.map(([id]) => ({ provider, index, id }))))(
  'submits only the chosen $id with its own endpoint and credential variable',
  async ({ provider, index, id }) => {
    const select = document.querySelector('#provider')
    select.value = String(index)
    select.dispatchEvent(new Event('change'))
    const model = document.querySelector('#model')
    model.value = id
    model.dispatchEvent(new Event('change'))
    expect(document.querySelector('.advanced').open).toBe(false)
    document.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }))
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('reply', {
      requestId: 'first', remember: false,
      value: { model_id: `openai:${id}`, api: provider.api, base_url: provider.url, api_key_env: provider.key },
    }))
  },
)

it('reveals custom fields and clears credentials from the previously selected service', async () => {
  const select = document.querySelector('#provider')
  select.value = 'custom'
  select.dispatchEvent(new Event('change'))
  expect(document.querySelector('.advanced').open).toBe(true)
  expect(document.querySelector('#model').hidden).toBe(true)
  expect(document.querySelector('#field-api_key_env').value).toBe('')
  document.querySelector('#field-model_id').value = 'ollama:qwen3.8'
  document.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }))
  await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('reply', {
    requestId: 'first', remember: false,
    value: { model_id: 'ollama:qwen3.8', api: 'chat', base_url: null, api_key_env: null },
  }))
})

it('preserves explicit advanced overrides', async () => {
  document.querySelector('#field-base_url').value = 'https://example.test/v1'
  document.querySelector('#field-model_id').value = 'openai:custom-model'
  document.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }))
  await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('reply', expect.objectContaining({
    value: { model_id: 'openai:custom-model', api: 'responses',
      base_url: 'https://example.test/v1', api_key_env: 'OPENAI_API_KEY' },
  })))
})

it('labels and protects provider credentials without saving by default', async () => {
  eventListener({ payload: { type: 'credentials_required', request_id: 'key', names: ['DEEPSEEK_API_KEY'] } })
  expect(window.scrollTo).toHaveBeenLastCalledWith(0, 0)
  const input = document.querySelector('#field-DEEPSEEK_API_KEY')
  expect(input.type).toBe('password')
  expect(document.querySelector('label').textContent).toBe('DeepSeek API Key')
  expect(document.querySelector('#remember').checked).toBe(false)
  input.value = 'not-a-real-key'
  document.querySelector('form').dispatchEvent(new Event('submit', { cancelable: true }))
  await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith('reply', {
    requestId: 'key', value: { values: { DEEPSEEK_API_KEY: 'not-a-real-key' } }, remember: false,
  }))
  await vi.waitFor(() => expect(input.value).toBe(''))
})
