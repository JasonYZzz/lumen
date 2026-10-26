// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { exchangeLaunchToken, lumenApi } from './client'
import { AuthenticationError } from './errors'

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState({}, '', '/') })

describe('browser authentication', () => {
  it('classifies a 401 and requests reconnection without retrying a mutation', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', {status: 401}))
    vi.stubGlobal('fetch', fetch)
    const expired = vi.fn()
    window.addEventListener('lumen:authentication-required', expired, {once: true})
    await expect(lumenApi.createSession()).rejects.toBeInstanceOf(AuthenticationError)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][1]).toMatchObject({credentials: 'include', method: 'POST'})
    expect(expired).toHaveBeenCalledOnce()
  })

  it('accepts a consumed launch link only when the browser is already authenticated', async () => {
    window.history.replaceState({}, '', '/?token=used&view=library&id=resource')
    const fetch = vi.fn().mockResolvedValueOnce(new Response('{}', {status: 401}))
      .mockResolvedValueOnce(new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    await exchangeLaunchToken()
    expect(window.location.search).toBe('?view=library&id=resource')
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/bootstrap')
  })

  it('rejects a consumed launch link without an authenticated cookie', async () => {
    window.history.replaceState({}, '', '/?token=used')
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response('{}', {status: 401})))
    await expect(exchangeLaunchToken()).rejects.toBeInstanceOf(AuthenticationError)
  })
})
