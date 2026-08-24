import { useState } from 'react'
import { api, type Me, type RuleResult } from '../api'
import { useAction, useAsync } from '../hooks'
import { Badge, Card, Empty, LaneBadge, SeverityBadge } from '../ui'

const SUMMARY_PREVIEW_LEN = 320

const DECISION_LABEL: Record<string, string> = {
  approve: 'Approved',
  'approve-conditions': 'Approved with Conditions',
  suspend: 'Suspended',
  deny: 'Denied',
}

function RuleRow({ r }: { r: RuleResult }) {
  // Three outcomes, never two. An indeterminate rule is not a pass, and
  // rendering it as one would be the easiest lie on this screen.
  const tone = r.outcome === 'pass' ? 'success' : r.outcome === 'fail' ? 'danger' : 'muted'
  const label = r.outcome === 'indeterminate' ? 'Not checked' : r.outcome === 'pass' ? 'Pass' : 'Fail'
  return (
    <div style={{ padding: '9px 0', borderBottom: '1px solid var(--neutral-100)' }}>
      <div className="chip-row" style={{ justifyContent: 'space-between' }}>
        <strong style={{ fontSize: 13 }}>{r.label}</strong>
        <Badge tone={tone}>{label}</Badge>
      </div>
      <div className="hint">{r.detail}</div>
      {r.evidence && <div className="mono-sm">{r.evidence}</div>}
    </div>
  )
}

export default function Hub({
  me,
  loanId,
  refreshKey,
  onChanged,
  onPick,
}: {
  me: Me
  loanId: string | null
  refreshKey: number
  onChanged: () => void
  onPick: (id: string) => void
}) {
  const { data: loans } = useAsync(() => api.loans(), [refreshKey])
  const id = loanId ?? loans?.find((l) => l.ready)?.id ?? loans?.[0]?.id ?? null
  const { data: hub, error } = useAsync(() => (id ? api.hub(id) : Promise.resolve(null)), [id, refreshKey])
  const [decision, setDecision] = useState('approve-conditions')
  const [note, setNote] = useState('')
  const { busy, message, perform } = useAction()
  const [summaryExpanded, setSummaryExpanded] = useState(false)

  if (error) return <Empty>{error}</Empty>
  if (!hub || !id) return <Empty>Loading…</Empty>

  const l = hub.loan
  const canDecide = me.can.includes('decide_loan')

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Underwriters&rsquo; Hub</h1>
          <p>
            {l.borrowers} · {l.program} {l.purpose} · ${l.amount.toLocaleString()} ·{' '}
            {l.metro}
          </p>
        </div>
        <select style={{ width: 240 }} value={id} onChange={(e) => onPick(e.target.value)}>
          {loans?.map((x) => (
            <option key={x.id} value={x.id}>
              {x.id} — {x.borrowers}
              {x.ready ? ' · ready' : ''}
            </option>
          ))}
        </select>
      </div>

      {l.summary && (
        <Card
          title="AI Summary"
          sub="Written by the Summarizer Agent from the file on record."
          style={{ marginBottom: 14 }}
        >
          <p style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>
            {summaryExpanded || l.summary.length <= SUMMARY_PREVIEW_LEN
              ? l.summary
              : l.summary.slice(0, SUMMARY_PREVIEW_LEN).replace(/\s+\S*$/, '') + '…'}
          </p>
          {l.summary.length > SUMMARY_PREVIEW_LEN && (
            <button className="btn sm" onClick={() => setSummaryExpanded((v) => !v)}>
              {summaryExpanded ? 'Show less' : 'Show full summary'}
            </button>
          )}
        </Card>
      )}

      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr 1fr', marginBottom: 14 }}>
        <Card title="Guideline flags" sub={hub.guidelines.label}>
          {(['dti', 'ltv', 'fico'] as const).map((key) => {
            const g = hub.guidelines[key]
            const cap = 'cap' in g ? g.cap : g.floor
            return (
              <div className="chip-row" key={key} style={{ justifyContent: 'space-between', padding: '6px 0' }}>
                <span style={{ textTransform: 'uppercase', fontSize: 12, fontWeight: 700 }}>
                  {key}
                </span>
                <span className="mono-sm">
                  {g.value} vs {cap}
                </span>
                <Badge tone={g.pass ? 'success' : 'danger'}>{g.pass ? 'Within' : 'Breach'}</Badge>
              </div>
            )
          })}
        </Card>

        <Card title="Automated underwriting" sub={`${hub.aus.engine} findings`}>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 20, fontWeight: 700 }}>
            {hub.aus.result}
          </div>
          <p className="hint">
            A Refer or Caution means the engine declined to approve; the file is manually
            underwritten against stricter guidelines.
          </p>
        </Card>

        <Card title="File state">
          <div className="chip-row" style={{ marginBottom: 8 }}>
            {l.ready ? <Badge tone="success">Ready for underwriting</Badge> : <Badge tone="warning">Not ready</Badge>}
            {l.decision && <Badge tone="success">{DECISION_LABEL[l.decision] ?? l.decision}</Badge>}
          </div>
          <div className="hint">
            {l.exceptions} exception(s) · {l.open_hitl} still with a person
          </div>
          <div className="hint" style={{ marginTop: 6 }}>
            Stage: {l.stage_name}
          </div>
        </Card>
      </div>

      <div className="grid" style={{ gridTemplateColumns: '1.1fr 1fr', marginBottom: 14 }}>
        <Card title="Rules engine" sub="Recomputed from the documents, not read from the application.">
          {hub.rules.map((r) => (
            <RuleRow key={r.rule_id} r={r} />
          ))}
        </Card>

        <div className="grid" style={{ gap: 14 }}>
          <Card title="Conditions" sub="Auto-checked as the underlying exception closes.">
            {hub.conditions.map((c, i) => (
              <div className="chip-row" key={i} style={{ padding: '5px 0' }}>
                <span style={{ color: c.done ? 'var(--teal-500)' : 'var(--neutral-400)' }}>
                  {c.done ? '☑' : '☐'}
                </span>
                <span style={{ fontSize: 13 }}>{c.text}</span>
              </div>
            ))}
          </Card>

          <Card title="Exceptions on this file">
            {hub.exceptions.length === 0 ? (
              <Empty>None raised.</Empty>
            ) : (
              hub.exceptions.map((e) => (
                <div key={e.id} style={{ padding: '7px 0', borderBottom: '1px solid var(--neutral-100)' }}>
                  <div className="chip-row" style={{ justifyContent: 'space-between' }}>
                    <span style={{ fontSize: 13 }}>{e.label}</span>
                    <SeverityBadge severity={e.severity} />
                  </div>
                  <div className="chip-row" style={{ marginTop: 4 }}>
                    <LaneBadge lane={e.lane} requiresSup={e.requires_sup} />
                    <Badge tone="muted">{e.status}</Badge>
                  </div>
                </div>
              ))
            )}
          </Card>
        </div>
      </div>

      <Card title="Decision" sub="Underwriters only, and only on a file the system considers ready.">
        {!canDecide ? (
          <p className="hint">
            Your role cannot render an underwriting decision. Sign in as the underwriter to
            do that.
          </p>
        ) : l.decision ? (
          <p>
            Decided <strong>{DECISION_LABEL[l.decision] ?? l.decision}</strong>. A decision is
            recorded once and cannot be overwritten.
          </p>
        ) : (
          <>
            <div className="grid" style={{ gridTemplateColumns: '220px 1fr auto', gap: 10, alignItems: 'end' }}>
              <div>
                <label className="field">Decision</label>
                <select value={decision} onChange={(e) => setDecision(e.target.value)}>
                  {hub.decisions.map((d) => (
                    <option key={d} value={d}>
                      {DECISION_LABEL[d] ?? d}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="field">Note</label>
                <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
              </div>
              <button
                className="btn coral"
                disabled={busy}
                onClick={() => void perform(() => api.decideLoan(id, decision, note), onChanged)}
              >
                Record decision
              </button>
            </div>
            {!l.ready && (
              <p className="hint" style={{ marginTop: 8 }}>
                This file is not ready. The server will refuse — open gating exceptions must
                be cleared first.
              </p>
            )}
          </>
        )}
        {message && (
          <p
            style={{
              marginTop: 8,
              fontSize: 12,
              color: message.refusal ? 'var(--status-danger)' : 'var(--neutral-700)',
            }}
          >
            {message.refusal ? 'Refused: ' : ''}
            {message.text}
          </p>
        )}
      </Card>
    </>
  )
}
