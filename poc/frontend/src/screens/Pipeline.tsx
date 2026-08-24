import { useEffect, useState } from 'react'
import { api, type Me } from '../api'
import { LogPane, type LogLine } from '../App'
import { useAsync } from '../hooks'
import { Badge, Card, Empty, ProgressBar } from '../ui'

export default function Pipeline({
  me,
  refreshKey,
  log,
  scanning,
  onScan,
  onOpenLoan,
}: {
  me: Me
  refreshKey: number
  log: LogLine[]
  scanning: boolean
  onScan: (id: string) => void
  onOpenLoan: (id: string) => void
}) {
  const { data: loans, error } = useAsync(() => api.loans(), [refreshKey])
  const { data: k } = useAsync(() => api.kpis(), [refreshKey])
  const [selected, setSelected] = useState<string | null>(null)
  const canScan = me.can.includes('start_scan')

  const target = selected ?? loans?.[0]?.id ?? null

  // Selecting a loan is the trigger — no separate click. The server decides
  // what, if anything, is outstanding for it and no-ops (with a log line
  // explaining why) when there's nothing left for the agents to do, so this
  // is safe to fire on every genuine selection change, including the loan
  // the dropdown defaults to on first load.
  useEffect(() => {
    if (canScan && target && !scanning) onScan(target)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target])

  if (error) return <Empty>{error}</Empty>
  if (!loans) return <Empty>Loading…</Empty>

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Loan Pipeline</h1>
          <p>
            {loans.length} loans · {loans.filter((l) => l.scanned).length} scanned ·{' '}
            {loans.filter((l) => l.ready).length} ready for underwriting
          </p>
        </div>
        <div className="chip-row">
          <select
            style={{ width: 200 }}
            value={target ?? ''}
            onChange={(e) => setSelected(e.target.value)}
          >
            {loans.map((l) => (
              <option key={l.id} value={l.id}>
                {l.id} — {l.borrowers}
              </option>
            ))}
          </select>
          {canScan ? (
            <button
              className="scan-btn"
              disabled={scanning || !target}
              onClick={() => target && onScan(target)}
            >
              {scanning ? 'Agents running…' : 'Run agents'}
            </button>
          ) : (
            // Not a disabled button: the role genuinely cannot do this, and a
            // greyed control invites someone to wonder why it will not work.
            <span className="hint">Only a supervisor can start a scan.</span>
          )}
        </div>
      </div>

      {k && (
        <div className="grid" style={{ gridTemplateColumns: 'repeat(5,1fr)', marginBottom: 14 }}>
          {k.by_stage.map((s) => (
            <Card key={s.stage}>
              <div className="hint">{s.name}</div>
              <div style={{ fontFamily: 'var(--font-display)', fontSize: 22, fontWeight: 700 }}>
                {s.loans}
              </div>
              <ProgressBar
                pct={k.loans ? (s.loans / k.loans) * 100 : 0}
                color="var(--navy-500)"
              />
            </Card>
          ))}
        </div>
      )}

      <div className="grid" style={{ gridTemplateColumns: '1.55fr 1fr' }}>
        <Card title="Book" sub="Click a loan to open it in the Underwriters' Hub.">
          <table>
            <thead>
              <tr>
                <th>Loan</th>
                <th>Borrowers</th>
                <th>Program</th>
                <th style={{ textAlign: 'right' }}>Amount</th>
                <th style={{ textAlign: 'right' }}>DTI</th>
                <th>Stage</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {loans.map((l) => (
                <tr key={l.id} className="clickable" onClick={() => onOpenLoan(l.id)}>
                  <td className="mono-sm">{l.id}</td>
                  <td>{l.borrowers}</td>
                  <td>
                    {l.program} {l.purpose}
                  </td>
                  <td style={{ textAlign: 'right' }} className="mono-sm">
                    ${l.amount.toLocaleString()}
                  </td>
                  <td style={{ textAlign: 'right' }} className="mono-sm">
                    {l.dti}%
                  </td>
                  <td className="hint">{l.stage_name}</td>
                  <td>
                    <span className="chip-row">
                      {l.decision && <Badge tone="success">{l.decision}</Badge>}
                      {!l.decision && l.ready && <Badge tone="success">Ready</Badge>}
                      {!l.ready && l.open_hitl > 0 && (
                        <Badge tone="warning">{l.open_hitl} with a human</Badge>
                      )}
                      {!l.scanned && <Badge tone="muted">Not scanned</Badge>}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card
          title="Live agent activity"
          sub="Every tool call, including the ones the gate refused."
        >
          <LogPane log={log} />
          <p className="hint" style={{ marginTop: 10, marginBottom: 0 }}>
            Refusals are shown in red. An agent being denied a tool it does not hold is
            the control working, not an error.
          </p>
        </Card>
      </div>
    </>
  )
}
