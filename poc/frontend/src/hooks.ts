import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './api'

/**
 * Fetch-on-mount with a manual refresh.
 *
 * `reload` is what every mutation calls after it succeeds. The alternative —
 * patching local state optimistically — would let the UI show an exception as
 * resolved while the server rolled the write back on an invariant violation,
 * which is precisely the disagreement this codebase spends its effort avoiding.
 * Re-reading is cheaper than being wrong.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const alive = useRef(true)

  const run = useCallback(async () => {
    setLoading(true)
    try {
      const result = await fn()
      if (alive.current) {
        setData(result)
        setError(null)
      }
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (alive.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    alive.current = true
    void run()
    return () => {
      alive.current = false
    }
  }, [run])

  return { data, error, loading, reload: run }
}

/** Surfaces a server refusal as written. See ApiError.isRefusal. */
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; refusal: boolean } | null>(null)

  const perform = useCallback(async (fn: () => Promise<unknown>, onDone?: () => void) => {
    setBusy(true)
    setMessage(null)
    try {
      await fn()
      onDone?.()
    } catch (e) {
      if (e instanceof ApiError) setMessage({ text: e.message, refusal: e.isRefusal })
      else setMessage({ text: e instanceof Error ? e.message : String(e), refusal: false })
    } finally {
      setBusy(false)
    }
  }, [])

  return { busy, message, perform, clear: () => setMessage(null) }
}
