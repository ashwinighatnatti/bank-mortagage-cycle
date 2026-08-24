/**
 * The shell: login, header, sidebar, screen routing, and the live agent log.
 *
 * ON HIDING THINGS. The sidebar hides Approvals from anyone who is not a
 * supervisor, and the scan button only renders for a role that may scan. That
 * is presentation, not security — every one of those actions is re-checked by
 * `rbac.require()` on the server before any write. The "Your access" card in
 * the sidebar shows the capability list the server returned, so what the UI
 * believes and what the server enforces are visibly the same thing.
 */

import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, setUnauthorizedHandler, token, type Me } from './api'
import { useAsync } from './hooks'
import { Avatar } from './ui'
import Dashboard from './screens/Dashboard'
import Pipeline from './screens/Pipeline'
import Exceptions from './screens/Exceptions'
import Hub from './screens/Hub'
import Approvals from './screens/Approvals'
import Audit from './screens/Audit'

export type Screen = 'dashboard' | 'pipeline' | 'exceptions' | 'hub' | 'approvals' | 'audit'

export interface LogLine {
  agent: string
  kind: string
  text: string
  tool: string | null
  ok: boolean
}

const AGENT_LABEL: Record<string, string> = {
  intake: 'Document Intake',
  validation: 'Validation',
  processing: 'Processing',
  summarizer: 'Summarizer',
  system: 'System',
}

/* ------------------------------------------------------------------ login
 *
 * Ported from the reference design: navy story panel on the left, the form on
 * the right, and the demo roster underneath it with a Use button per row.
 *
 * The reference also carried a "Narrated Tour" button. There is no narrated
 * tour in this build, and a control that does nothing is worse than an absent
 * one, so it is not here.
 */
const DEMO_PASSWORD = 'Coforge@123'

const ROLE_LABEL: Record<string, string> = {
  analyst: 'Pre-underwriting Analyst',
  supervisor: 'Supervisor',
  underwriter: 'Underwriter',
}

function Login({ onIn }: { onIn: (me: Me) => void }) {
  const { data: personas } = useAsync(() => api.personas(), [])
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState(DEMO_PASSWORD)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const signIn = async (user: string, pass: string) => {
    if (!user.trim()) {
      setError('Enter a username, or pick one from the list below.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const r = await api.login(user.trim(), pass)
      token.set(r.token)
      onIn(r.user)
    } catch (e) {
      // The server deliberately gives the same answer for a wrong password and
      // an unknown username, so don't invent a more specific one here.
      setError(
        e instanceof ApiError && e.statusCode === 401
          ? 'That username and password do not match an account.'
          : e instanceof Error
            ? e.message
            : String(e),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="signin-wrap">
      <div className="signin-card">
        <div className="signin-story">
          <div className="top">
            <div className="brand-mark" aria-hidden>
              X
            </div>
            <span className="wordmark">Coforge</span>
          </div>

          <h1>AI-Native Mortgage</h1>
          <p className="lede">
            Origination to Post-Closing, run by an agentic pipeline with
            Human-in-the-Loop governance and role-based control.
          </p>

          <ul className="signin-points">
            <li>
              <span className="dot" style={{ background: 'var(--coral-500)' }} />
              Agents classify, extract, validate &amp; auto-repair loan files
            </li>
            <li>
              <span className="dot" style={{ background: 'var(--teal-500)' }} />
              Judgment calls routed to analyst HITL queues
            </li>
            <li>
              <span className="dot" style={{ background: 'var(--gold-400)' }} />
              Clean files arrive at the Underwriters&rsquo; Digital Hub
            </li>
          </ul>
        </div>

        <form
          className="signin-form"
          onSubmit={(e) => {
            e.preventDefault()
            void signIn(username, password)
          }}
        >
          <h2>Sign in</h2>
          <p className="sub">Use a demo account to enter the console.</p>

          <label className="field" htmlFor="username">
            Username
          </label>
          <input
            id="username"
            type="text"
            autoComplete="username"
            placeholder="analyst1"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
          />

          <label className="field" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            placeholder={DEMO_PASSWORD}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />

          <button className="btn-block coral" type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>

          {error && <div className="signin-error">{error}</div>}

          <div className="creds">
            <div className="creds-head">
              Demo credentials &middot; password {DEMO_PASSWORD}
            </div>
            {personas?.map((p) => (
              <div className="cred-row" key={p.username}>
                <Avatar name={p.name} />
                <span className="who">
                  <b>{p.name}</b>
                  <span>
                    {p.username} &middot; {ROLE_LABEL[p.role] ?? p.role}
                    {p.queue ? ` · Queue ${p.queue}` : ''}
                  </span>
                </span>
                <button
                  type="button"
                  className="use"
                  disabled={busy}
                  onClick={() => {
                    setUsername(p.username)
                    setPassword(DEMO_PASSWORD)
                    void signIn(p.username, DEMO_PASSWORD)
                  }}
                >
                  Use
                </button>
              </div>
            ))}
          </div>
        </form>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------- shell */
export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [ready, setReady] = useState(false)
  const [screen, setScreen] = useState<Screen>('dashboard')
  const [loanId, setLoanId] = useState<string | null>(null)
  const [log, setLog] = useState<LogLine[]>([])
  const [scanning, setScanning] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  // A rejected token means "show the login screen", not "reload the page".
  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null))
    return () => setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    if (!token.get()) {
      setReady(true)
      return
    }
    api
      .me()
      .then(setMe)
      .catch(() => token.clear())
      .finally(() => setReady(true))
  }, [])

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), [])

  // Guarded on `me`. Hooks cannot sit behind the `if (!me) return <Login/>`
  // below, so without this the logged-out page calls an authenticated endpoint
  // on every render pass and gets a 401 for its trouble.
  const { data: kpis } = useAsync(
    () => (me ? api.kpis() : Promise.resolve(null)),
    [me, refreshKey],
  )

  const openLoan = useCallback((id: string) => {
    setLoanId(id)
    setScreen('hub')
  }, [])

  const startScan = useCallback(
    (id: string) => {
      setLog([])
      setScanning(true)
      const source = new EventSource(api.scanUrl(id))
      source.addEventListener('log', (ev) => {
        setLog((prev) => [...prev, JSON.parse((ev as MessageEvent).data) as LogLine])
      })
      source.addEventListener('complete', () => {
        source.close()
        setScanning(false)
        refresh()
      })
      source.onerror = () => {
        source.close()
        setScanning(false)
        // A 403 arrives here as a generic connection failure — EventSource does
        // not expose the status code. Say what actually happened rather than
        // "connection lost", which would send someone debugging the network.
        setLog((prev) => [
          ...prev,
          {
            agent: 'system',
            kind: 'error',
            text: 'Scan stream ended. If you are not a supervisor, the server refused it.',
            tool: null,
            ok: false,
          },
        ])
        refresh()
      }
    },
    [refresh],
  )

  if (!ready) return null
  if (!me) return <Login onIn={setMe} />

  const can = (a: string) => me.can.includes(a)

  const nav: { id: Screen; label: string; metric?: string | number; show: boolean }[] = [
    { id: 'dashboard', label: 'Dashboard', metric: kpis?.loans, show: true },
    { id: 'pipeline', label: 'Loan Pipeline', metric: kpis?.scanned, show: true },
    { id: 'exceptions', label: 'AI Exceptions & HITL', metric: kpis?.open_hitl, show: true },
    { id: 'hub', label: "Underwriters' Hub", metric: kpis?.ready, show: true },
    {
      id: 'approvals',
      label: 'Approvals',
      metric: kpis?.pending_approvals,
      show: can('view_approvals'),
    },
    { id: 'audit', label: 'Audit Trail', show: true },
  ]

  const body = () => {
    switch (screen) {
      case 'dashboard':
        return <Dashboard refreshKey={refreshKey} onOpenLoan={openLoan} />
      case 'pipeline':
        return (
          <Pipeline
            me={me}
            refreshKey={refreshKey}
            log={log}
            scanning={scanning}
            onScan={startScan}
            onOpenLoan={openLoan}
          />
        )
      case 'exceptions':
        return <Exceptions me={me} refreshKey={refreshKey} onChanged={refresh} onOpenLoan={openLoan} />
      case 'hub':
        return (
          <Hub
            me={me}
            loanId={loanId}
            refreshKey={refreshKey}
            onChanged={refresh}
            onPick={setLoanId}
          />
        )
      case 'approvals':
        return <Approvals refreshKey={refreshKey} onChanged={refresh} />
      case 'audit':
        return <Audit refreshKey={refreshKey} />
    }
  }

  return (
    <>
      <header className="header">
        <div className="brand">
          <span className="dot" /> Coforge · AI-Native Mortgage
        </div>
        <span className="spacer" />
        {scanning && <span className="hint" style={{ color: '#9FB2D6' }}>Agents running…</span>}
        <select
          className="role-select"
          value={me.username}
          onChange={async (e) => {
            // Role switching without re-auth is a demo affordance, kept from the
            // reference. It is a real login: a new token, a new server-side role.
            const r = await api.login(e.target.value, 'Coforge@123')
            token.set(r.token)
            setMe(r.user)
            setScreen('dashboard')
            refresh()
          }}
        >
          {['analyst1', 'analyst2', 'analyst3', 'supervisor', 'underwriter'].map((u) => (
            <option key={u} value={u}>
              {u}
            </option>
          ))}
        </select>
        <Avatar name={me.name} />
        <button
          className="btn sm"
          onClick={() => {
            token.clear()
            setMe(null)
          }}
        >
          Sign out
        </button>
      </header>

      <div className="layout">
        <aside className="sidebar">
          {nav
            .filter((n) => n.show)
            .map((n) => (
              <button
                key={n.id}
                className={`side-item ${screen === n.id ? 'active' : ''}`}
                onClick={() => setScreen(n.id)}
              >
                <span>{n.label}</span>
                {n.metric !== undefined && <span className="metric">{n.metric ?? '–'}</span>}
              </button>
            ))}

          <div className="access-card">
            <h4>Your access</h4>
            <div style={{ fontWeight: 700 }}>{me.name}</div>
            <div className="hint" style={{ marginBottom: 8 }}>
              {me.role}
              {me.queue ? ` · queue ${me.queue}` : ''}
            </div>
            <div className="can-list">
              {me.can.map((c) => (
                <span className="tag" key={c}>
                  {c}
                </span>
              ))}
            </div>
            <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
              Enforced server-side, not by hiding buttons.
            </p>
          </div>
        </aside>

        <main className="content">
          <div className="content-inner">{body()}</div>
        </main>
      </div>
    </>
  )
}

export function LogPane({ log }: { log: LogLine[] }) {
  if (log.length === 0)
    return (
      <div className="logpane">
        <div className="logline">
          <span className="msg" style={{ color: 'var(--navy-300)' }}>
            No agent activity yet. A supervisor can start a scan from the pipeline.
          </span>
        </div>
      </div>
    )
  return (
    <div className="logpane">
      {log.map((l, i) => (
        <div className={`logline ${l.kind}`} key={i}>
          <span className="who">{AGENT_LABEL[l.agent] ?? l.agent}</span>
          <span className="msg">
            {l.tool ? `${l.kind === 'denied' ? 'DENIED ' : ''}${l.tool} — ` : ''}
            {l.text}
          </span>
        </div>
      ))}
    </div>
  )
}
