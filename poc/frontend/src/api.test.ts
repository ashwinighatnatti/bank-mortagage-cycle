/**
 * Tests for the API client's auth behaviour.
 *
 * These exist because of a real bug: the client used to call
 * `window.location.reload()` on any 401, and the app fetched an authenticated
 * endpoint while logged out. Together those reloaded the document in a tight
 * loop and the page looked like it was flickering. Nothing in a typecheck or a
 * build catches that — only exercising the client does.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, setUnauthorizedHandler, token } from './api'

function memoryStorage(): Storage {
  const map = new Map<string, string>()
  return {
    get length() {
      return map.size
    },
    clear: () => map.clear(),
    getItem: (k: string) => map.get(k) ?? null,
    key: (i: number) => [...map.keys()][i] ?? null,
    removeItem: (k: string) => void map.delete(k),
    setItem: (k: string, v: string) => void map.set(k, v),
  }
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  ;(globalThis as { localStorage: Storage }).localStorage = memoryStorage()
  setUnauthorizedHandler(null)
  vi.restoreAllMocks()
})

describe('when logged out', () => {
  it('does not even attempt an authenticated request', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')

    await expect(api.kpis()).rejects.toBeInstanceOf(ApiError)
    // The whole point: no round trip, so no 401, so nothing to react to.
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('still allows the two endpoints that need no token', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(200, [{ username: 'supervisor' }]))

    await expect(api.personas()).resolves.toHaveLength(1)
    expect(fetchSpy).toHaveBeenCalledOnce()

    fetchSpy.mockResolvedValue(jsonResponse(200, { token: 't', user: { name: 'x' } }))
    await expect(api.login('supervisor', 'Coforge@123')).resolves.toMatchObject({
      token: 't',
    })
  })

  it('sends no Authorization header on an unauthenticated call', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(200, []))

    await api.personas()
    const headers = (fetchSpy.mock.calls[0][1] as RequestInit).headers as Headers
    expect(headers.has('Authorization')).toBe(false)
  })
})

describe('when the server rejects the session', () => {
  beforeEach(() => token.set('stale-token'))

  it('clears the token and hands control back without reloading', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(401, { detail: 'invalid token' }),
    )
    const onExpired = vi.fn()
    setUnauthorizedHandler(onExpired)

    await expect(api.kpis()).rejects.toMatchObject({ statusCode: 401 })

    expect(token.get()).toBeNull()
    expect(onExpired).toHaveBeenCalledOnce()
  })

  it('cannot loop, because the second call never reaches the network', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(401, { detail: 'invalid token' }))
    setUnauthorizedHandler(vi.fn())

    await expect(api.kpis()).rejects.toThrow()
    await expect(api.kpis()).rejects.toThrow()
    await expect(api.loans()).rejects.toThrow()

    // One real request. The token was cleared by it, so the rest failed locally.
    expect(fetchSpy).toHaveBeenCalledOnce()
  })
})

describe('refusals', () => {
  beforeEach(() => token.set('good-token'))

  it('surfaces a 403 exactly as the server worded it', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(403, { detail: 'analyst may not view_approvals' }),
    )

    await expect(api.approvals()).rejects.toSatisfy(
      (e: ApiError) => e.isRefusal && e.message === 'analyst may not view_approvals',
    )
  })

  it('does not mistake a 404 for a refusal', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(404, { detail: 'no such loan: LN-NOPE' }),
    )

    await expect(api.hub('LN-NOPE')).rejects.toSatisfy(
      (e: ApiError) => !e.isRefusal && e.statusCode === 404,
    )
  })

  it('sends the bearer token when it has one', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(200, {}))

    await api.kpis()
    const headers = (fetchSpy.mock.calls[0][1] as RequestInit).headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer good-token')
  })
})
