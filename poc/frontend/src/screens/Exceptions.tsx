import { useState } from 'react'
import { api, type ExceptionView, type Me } from '../api'
import { useAction, useAsync } from '../hooks'
import { Badge, Card, Empty, LaneBadge, SeverityBadge } from '../ui'

const ACTIONS = [
  { id: 'verify', label: 'Verify & approve' },
  { id: 'recalc', label: 'Recalculate' },
  { id: 'request', label: 'Request document' },
  { id: 'override', label: 'Override' },
]

/**
 * Role-adaptive, as in the reference: an analyst lands on their own queue, a
 * supervisor gets the whole board with filters. Both can *see* everything —
 * only acting is queue-scoped, and that is enforced server-side.
 */
export default function Exceptions({
  me,
  refreshKey,
  onChanged,
  onOpenLoan,
}: {
  me: Me
  refreshKey: number
  onChanged: () => void
  onOpenLoan: (id: string) => void
}) {
  const [queue, setQueue] = useState<string>(me.queue ?? '')
  const [severity, setSeverity] = useState('')
  const [status, setStatus] = useState('')
  const { data, error } = useAsync(
    () => api.exceptions({ queue: queue || undefined, severity: severity || undefined, status: status || undefined }),
    [refreshKey, queue, severity, status],
  )

  if (error) return <Empty>{error}</Empty>

  const rows = data ?? []
  const open = rows.filter((e) => !['resolved', 'approved'].includes(e.status))
  const closed = rows.filter((e) => ['resolved', 'approved'].includes(e.status))

  return (
    <>
      <div className="page-head">
        <div>
          <h1>AI Exceptions &amp; HITL</h1>
          <p>
            {open.length} open · {closed.length} closed
            {me.queue ? ` · you hold queue ${me.queue}` : ''}
          </p>
        </div>
        <div className="chip-row">
          <select style={{ width: 130 }} value={queue} onChange={(e) => setQueue(e.target.value)}>
            <option value="">All queues</option>
            {['A', 'B', 'C'].map((q) => (
              <option key={q} value={q}>
                Queue {q}
              </option>
            ))}
          </select>
          <select style={{ width: 130 }} value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="">All severities</option>
            {['Critical', 'High', 'Medium', 'Low'].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <select style={{ width: 140 }} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Any status</option>
            {['routed', 'inqueue', 'pending', 'resolved', 'approved', 'predicted'].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </div>
      </div>

      {rows.length === 0 ? (
        <Empty>No exceptions match. Run a scan from the pipeline.</Empty>
      ) : (
        <div className="grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
          {rows.map((e) => (
            <ExceptionCard
              key={e.id}
              exc={e}
              me={me}
              onChanged={onChanged}
              onOpenLoan={onOpenLoan}
            />
          ))}
        </div>
      )}
    </>
  )
}

function ExceptionCard({
  exc,
  me,
  onChanged,
  onOpenLoan,
}: {
  exc: ExceptionView
  me: Me
  onChanged: () => void
  onOpenLoan: (id: string) => void
}) {
  const [action, setAction] = useState(ACTIONS[0].id)
  const [note, setNote] = useState('')
  const { busy, message, perform } = useAction()

  const closed = ['resolved', 'approved'].includes(exc.status)
  const mine = me.role !== 'analyst' || exc.queue === me.queue
  const canWork = me.can.includes('work_exception') && exc.lane === 'hitl' && !closed

  return (
    <Card>
      <div className="chip-row" style={{ justifyContent: 'space-between' }}>
        <strong>{exc.label}</strong>
        <SeverityBadge severity={exc.severity} />
      </div>
      <div className="chip-row" style={{ margin: '8px 0' }}>
        <LaneBadge lane={exc.lane} requiresSup={exc.requires_sup} />
        {exc.queue && <Badge tone="navy">Queue {exc.queue}</Badge>}
        <Badge tone={closed ? 'success' : 'muted'}>{exc.status}</Badge>
        <button className="btn sm" onClick={() => onOpenLoan(exc.loan_id)}>
          {exc.loan_id}
        </button>
      </div>

      <div style={{ fontSize: 12, color: 'var(--neutral-700)' }}>
        <div style={{ marginBottom: 6 }}>
          <strong>Confidence {exc.confidence}%</strong>
          {exc.confidence_revised_from != null && (
            <span style={{ color: 'var(--status-danger)' }}>
              {' '}
              — revised down from {exc.confidence_revised_from}
            </span>
          )}
          <div className="hint">{exc.disposition_reason}</div>
        </div>
        {exc.rationale && <p style={{ margin: '6px 0' }}>{exc.rationale}</p>}
        {exc.evidence && (
          <p className="mono-sm" style={{ margin: '6px 0', color: 'var(--navy-500)' }}>
            “{exc.evidence}”
            {exc.evidence_doc_id ? ` — ${exc.evidence_doc_id}` : ''}
          </p>
        )}
        {exc.recommendation && (
          <p style={{ margin: '6px 0' }}>
            <strong>AI recommends:</strong> {exc.recommendation}
          </p>
        )}
        {exc.revision_reason && (
          <p className="hint" style={{ color: 'var(--status-danger)' }}>
            {exc.revision_reason}
          </p>
        )}
        {closed && exc.resolution_note && (
          <p className="hint">
            {exc.resolution_note}
            {exc.resolved_by ? ` — ${exc.resolved_by}` : ''}
          </p>
        )}
      </div>

      {canWork && (
        <div style={{ marginTop: 12, borderTop: '1px solid var(--neutral-100)', paddingTop: 12 }}>
          {!mine ? (
            <p className="hint">
              Queue {exc.queue} belongs to another analyst. You can read it; the server
              will refuse an action.
            </p>
          ) : (
            <>
              <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <div>
                  <label className="field">Action</label>
                  <select value={action} onChange={(e) => setAction(e.target.value)}>
                    {ACTIONS.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="field">Note</label>
                  <input
                    type="text"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="What you did, and why"
                  />
                </div>
              </div>
              <button
                className="btn primary"
                style={{ marginTop: 8 }}
                disabled={busy}
                onClick={() => void perform(() => api.act(exc.id, action, note), onChanged)}
              >
                {exc.requires_sup ? 'Propose for sign-off' : 'Resolve'}
              </button>
              {exc.requires_sup && (
                <span className="hint" style={{ marginLeft: 10 }}>
                  A supervisor closes this one.
                </span>
              )}
            </>
          )}
          {message && (
            <p
              style={{
                marginTop: 8,
                marginBottom: 0,
                fontSize: 12,
                color: message.refusal ? 'var(--status-danger)' : 'var(--neutral-700)',
              }}
            >
              {message.refusal ? 'Refused: ' : ''}
              {message.text}
            </p>
          )}
        </div>
      )}
    </Card>
  )
}
