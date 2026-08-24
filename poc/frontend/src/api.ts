/**
 * The API client.
 *
 * One place that knows about tokens and error shapes. A 403 from this server is
 * always an RBAC refusal with a readable reason, so it is surfaced verbatim
 * rather than replaced with "something went wrong" — the whole point of the
 * server-side check is that the refusal explains itself.
 */

export type Role = 'analyst' | 'supervisor' | 'underwriter'

export interface Me {
  username: string
  name: string
  role: Role
  queue: string | null
  can: string[]
}

export interface LoanSummary {
  id: string
  borrowers: string
  metro: string
  program: string
  purpose: string
  amount: number
  fico: number
  dti: number
  ltv: number
  is_jumbo: boolean
  scanned: boolean
  ready: boolean
  decision: string | null
  delivered: boolean
  stage: number
  stage_name: string
  exceptions: number
  open_hitl: number
  summary: string | null
}

export interface ExceptionView {
  id: string
  loan_id: string
  stage: number
  stage_name: string
  type: string
  label: string
  severity: 'Low' | 'Medium' | 'High' | 'Critical'
  confidence: number
  lane: 'auto' | 'hitl'
  queue: string | null
  requires_sup: boolean
  status: string
  disposition_reason: string
  recommendation: string | null
  rationale: string | null
  evidence: string | null
  evidence_doc_id: string | null
  raised_by: string | null
  raised_at: string
  resolved_by: string | null
  resolution_note: string | null
  confidence_revised_from: number | null
  revision_reason: string | null
}

export interface RuleResult {
  rule_id: string
  label: string
  outcome: 'pass' | 'fail' | 'indeterminate'
  detail: string
  evidence: string | null
  missing: string[]
}

export interface Kpis {
  loans: number
  scanned: number
  predicted: number
  exceptions: number
  auto_total: number
  auto_repaired: number
  auto_pct: number
  resolved: number
  open_hitl: number
  pending_approvals: number
  ready: number
  decided: number
  delivered: number
  stp_pct: number
  cycle_days: number
  queue_load: Record<string, number>
  by_severity: Record<string, number>
  by_stage: { stage: number; name: string; loans: number }[]
}

export interface Approval {
  id: string
  exception_id: string
  loan_id: string
  exception_type: string
  proposed_by: string
  proposed_action: string
  ai_recommendation: string | null
  queue: string | null
  status: string
  decided_by: string | null
  note: string | null
  created_at: string
}

export interface ConfirmationRow {
  token: string
  loan_id: string
  tool: string
  args: Record<string, unknown>
  requested_by: string
  requested_at: string
  status: string
  confirmed_by: string | null
}

export interface AuditEntry {
  id: number
  at: string
  actor: string
  role: string
  kind: 'ai' | 'human' | 'system'
  action: string
  case_id: string
  run_id: string | null
  detail: Record<string, unknown>
  hash: string
}

export interface Hub {
  loan: LoanSummary
  guidelines: {
    program: string
    label: string
    dti: { value: number; cap: number; pass: boolean }
    ltv: { value: number; cap: number; pass: boolean }
    fico: { value: number; floor: number; pass: boolean }
    cu_threshold: number
  }
  aus: { engine: string; result: string }
  conditions: { text: string; done: boolean; exception_id: string | null }[]
  rules: RuleResult[]
  exceptions: ExceptionView[]
  decisions: string[]
}

export interface RunRow {
  run_id: string
  agent: string
  loan_id: string
  status: string
  tool_calls: number
  usd: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  started_at: string
}

const TOKEN_KEY = 'coforge.token'

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export class ApiError extends Error {
  constructor(readonly statusCode: number, message: string) {
    super(message)
  }
  /** A refusal the server explained. Show it to the user as written. */
  get isRefusal() {
    return this.statusCode === 403
  }
}

/**
 * Called when the server says the session is no longer valid. The app sets this
 * to "show the login screen".
 *
 * It used to be `window.location.reload()`, which was a bug with teeth: a
 * reload only helps if the 401 will not happen again on the next load, and if it
 * does, the page reloads in a tight loop and looks like it is flickering. Never
 * recover from a failed request by reloading the document — hand control back to
 * the app and let it render the state that is actually true.
 */
type AuthListener = () => void
let onUnauthorized: AuthListener | null = null

export function setUnauthorizedHandler(fn: AuthListener | null): void {
  onUnauthorized = fn
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  requiresAuth = true,
): Promise<T> {
  const t = token.get()

  // Fail locally rather than making a round trip that can only 401. This is
  // what makes the reload loop above structurally impossible: with no token
  // there is no request, so there is no 401 and nothing to react to.
  if (requiresAuth && !t) throw new ApiError(401, 'not authenticated')

  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (t) headers.set('Authorization', `Bearer ${t}`)

  const res = await fetch(`/api${path}`, { ...init, headers })
  if (res.status === 401) {
    token.clear()
    onUnauthorized?.()
    throw new ApiError(401, 'not authenticated')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* a non-JSON error body is still an error */
    }
    throw new ApiError(res.status, detail)
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

const get = <T,>(p: string) => request<T>(p)
const post = <T,>(p: string, body?: unknown) =>
  request<T>(p, { method: 'POST', body: body ? JSON.stringify(body) : undefined })

export const api = {
  // The two endpoints that work without a token.
  login: (username: string, password: string) =>
    request<{ token: string; user: Me }>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ username, password }) },
      false,
    ),
  personas: () =>
    request<{ username: string; name: string; role: Role; queue: string | null }[]>(
      '/auth/personas',
      {},
      false,
    ),

  me: () => get<Me>('/auth/me'),

  kpis: () => get<Kpis>('/kpis'),
  loans: () => get<LoanSummary[]>('/loans'),
  hub: (id: string) => get<Hub>(`/loans/${id}`),
  exceptions: (q: { queue?: string; status?: string; severity?: string } = {}) => {
    const p = new URLSearchParams()
    Object.entries(q).forEach(([k, v]) => v && p.set(k, v))
    return get<ExceptionView[]>(`/exceptions${p.toString() ? `?${p}` : ''}`)
  },
  approvals: () => get<Approval[]>('/approvals'),
  confirmations: () => get<ConfirmationRow[]>('/confirmations'),
  audit: (q: { case_id?: string; kind?: string } = {}) => {
    const p = new URLSearchParams()
    Object.entries(q).forEach(([k, v]) => v && p.set(k, v))
    return get<{ chain_intact: boolean; first_broken_hash: string | null; entries: AuditEntry[] }>(
      `/audit${p.toString() ? `?${p}` : ''}`,
    )
  },
  runs: () => get<RunRow[]>('/runs'),

  claim: (id: string) => post<ExceptionView>(`/exceptions/${id}/claim`),
  act: (id: string, action: string, note: string) =>
    post<ExceptionView>(`/exceptions/${id}/act`, { action, note }),
  decideApproval: (id: string, decision: string, note: string) =>
    post<{ id: string; status: string }>(`/approvals/${id}/decide`, { decision, note }),
  decideConfirmation: (tok: string, decision: string) =>
    post<{ token: string; status: string }>(`/confirmations/${encodeURIComponent(tok)}/decide`, {
      decision,
    }),
  decideLoan: (id: string, decision: string, note: string) =>
    post<LoanSummary>(`/loans/${id}/decision`, { decision, note }),

  /**
   * SSE. EventSource cannot set an Authorization header, so the token goes in
   * the query string — see the note on the server endpoint about why that is
   * acceptable here and what it would need before production.
   */
  scanUrl: (loanId: string) => `/api/loans/${loanId}/scan?token=${encodeURIComponent(token.get() ?? '')}`,
}
