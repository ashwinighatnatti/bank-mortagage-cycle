import { useState } from 'react'
import { api } from '../api'
import { useAction, useAsync } from '../hooks'
import { Badge, Card, Empty } from '../ui'
import type { ConfirmationRow } from '../api'

/**
 * Say what the action IS, not which function was called.
 *
 * Two requests on the same loan differing only in `service` are two different
 * commitments of money. Naming both of them "order_vendor_service" hides the
 * only thing the approver needs to see.
 */
const SERVICE_LABEL: Record<string, string> = {
  appraisal: 'Order a full appraisal',
  title: 'Order a title report',
  field_review: 'Order an appraisal field review',
  flood_determination: 'Order a flood determination',
  credit_repull: 'Re-pull the credit report',
  avm: 'Order an automated valuation',
}

export function describe(tool: string, args: Record<string, unknown>): string {
  if (tool === 'order_vendor_service') {
    const service = String(args.service ?? '')
    const vendor = args.vendor ? ` from ${String(args.vendor)}` : ''
    return (SERVICE_LABEL[service] ?? `Order ${service || 'a vendor service'}`) + vendor
  }
  if (tool === 'request_borrower_document') {
    return `Contact the borrower for a ${String(args.document_kind ?? 'document').replace(/_/g, ' ')}`
  }
  return tool
}

/** Group by file, so two actions on one loan read as two actions, not a repeat. */
export function groupByLoan(rows: ConfirmationRow[]): [string, ConfirmationRow[]][] {
  const byLoan = new Map<string, ConfirmationRow[]>()
  for (const r of rows) {
    const list = byLoan.get(r.loan_id) ?? []
    list.push(r)
    byLoan.set(r.loan_id, list)
  }
  return [...byLoan.entries()].sort(([a], [b]) => a.localeCompare(b))
}

/**
 * The supervisor inbox. Two queues that look similar and are not:
 *
 *   Sign-offs   — an analyst proposed a fix to a judgment-call exception.
 *   Gated calls — an agent tried to spend money or contact a borrower.
 *
 * The second is the one worth watching in a demo. Approving it does not run
 * anything; it records the authorisation, and the *next* agent run finds the
 * token in its context and the identical call then passes the gate.
 */
export default function Approvals({
  refreshKey,
  onChanged,
}: {
  refreshKey: number
  onChanged: () => void
}) {
  const { data: approvals, error, reload } = useAsync(() => api.approvals(), [refreshKey])
  const { data: confirmations, reload: reloadConf } = useAsync(
    () => api.confirmations(),
    [refreshKey],
  )
  const { busy, message, perform } = useAction()
  const [notes, setNotes] = useState<Record<string, string>>({})

  if (error) return <Empty>{error}</Empty>

  const pending = (approvals ?? []).filter((a) => a.status === 'pending')
  const decided = (approvals ?? []).filter((a) => a.status !== 'pending')
  const pendingConf = (confirmations ?? []).filter((c) => c.status === 'pending')
  const after = () => {
    void reload()
    void reloadConf()
    onChanged()
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Approvals</h1>
          <p>
            {pending.length} sign-off(s) · {pendingConf.length} gated action(s) awaiting
            authorisation
          </p>
        </div>
      </div>

      <Card
        title="Gated agent actions"
        sub="Money leaving the building, or a borrower being contacted. Bound to the exact arguments."
        style={{ marginBottom: 14 }}
      >
        {pendingConf.length === 0 ? (
          <Empty>Nothing waiting.</Empty>
        ) : (
          groupByLoan(pendingConf).map(([loanId, rows]) => (
            <div key={loanId} style={{ marginBottom: 14 }}>
              <div className="chip-row" style={{ marginBottom: 6 }}>
                <Badge tone="warning">{loanId}</Badge>
                <span className="hint">
                  {rows.length} separate action{rows.length > 1 ? 's' : ''} on this file
                  {rows.length > 1 && ' — each authorised on its own'}
                </span>
              </div>
              {rows.map((c) => (
                <div
                  key={c.token}
                  style={{
                    padding: '10px 12px',
                    marginBottom: 6,
                    border: '1px solid var(--neutral-200)',
                    borderRadius: 'var(--radius-md)',
                  }}
                >
                  {/* Lead with what actually differs. These requests share a
                      tool name and a loan id, so a card headed with either of
                      those reads as a duplicate of the one above it — and a
                      supervisor who cannot tell two authorisations apart at a
                      glance will approve both without reading. */}
                  <strong>{describe(c.tool, c.args)}</strong>
                  <div className="hint" style={{ marginTop: 2 }}>
                    Requested by the {c.requested_by} agent
                    {c.args.reason ? ` — ${String(c.args.reason)}` : ''}
                  </div>
                  <details style={{ marginTop: 6 }}>
                    <summary className="hint" style={{ cursor: 'pointer' }}>
                      Exact arguments being authorised
                    </summary>
                    <div className="mono-sm" style={{ marginTop: 4 }}>
                      {Object.entries(c.args).map(([k, v]) => (
                        <div key={k}>
                          {k} = {String(v)}
                        </div>
                      ))}
                    </div>
                  </details>
                  <div className="chip-row" style={{ marginTop: 8 }}>
                    <button
                      className="btn primary sm"
                      disabled={busy}
                      onClick={() =>
                        void perform(() => api.decideConfirmation(c.token, 'approved'), after)
                      }
                    >
                      Authorise
                    </button>
                    <button
                      className="btn danger sm"
                      disabled={busy}
                      onClick={() =>
                        void perform(() => api.decideConfirmation(c.token, 'rejected'), after)
                      }
                    >
                      Refuse
                    </button>
                    <span className="hint">
                      Covers these arguments only; the agent performs it on its next run.
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ))
        )}
      </Card>

      <Card title="Analyst proposals awaiting sign-off" style={{ marginBottom: 14 }}>
        {pending.length === 0 ? (
          <Empty>Nothing waiting.</Empty>
        ) : (
          pending.map((a) => (
            <div key={a.id} style={{ padding: '10px 0', borderBottom: '1px solid var(--neutral-100)' }}>
              <div className="chip-row" style={{ justifyContent: 'space-between' }}>
                <strong>
                  {a.id} · {a.exception_type}
                </strong>
                <span className="chip-row">
                  <Badge tone="navy">{a.loan_id}</Badge>
                  {a.queue && <Badge tone="muted">Queue {a.queue}</Badge>}
                </span>
              </div>
              <div style={{ fontSize: 13, marginTop: 4 }}>
                <strong>{a.proposed_by}</strong> proposes: {a.proposed_action}
              </div>
              {a.ai_recommendation && (
                <div className="hint">AI recommended: {a.ai_recommendation}</div>
              )}
              {a.note && <div className="hint">Note: {a.note}</div>}
              <div className="chip-row" style={{ marginTop: 8 }}>
                <input
                  type="text"
                  style={{ maxWidth: 340 }}
                  placeholder="Reason (shown on a rejection)"
                  value={notes[a.id] ?? ''}
                  onChange={(e) => setNotes({ ...notes, [a.id]: e.target.value })}
                />
                <button
                  className="btn primary sm"
                  disabled={busy}
                  onClick={() =>
                    void perform(() => api.decideApproval(a.id, 'approved', notes[a.id] ?? ''), after)
                  }
                >
                  Approve
                </button>
                <button
                  className="btn danger sm"
                  disabled={busy}
                  onClick={() =>
                    void perform(() => api.decideApproval(a.id, 'rejected', notes[a.id] ?? ''), after)
                  }
                >
                  Reject
                </button>
              </div>
            </div>
          ))
        )}
        {message && (
          <p
            style={{
              marginTop: 10,
              fontSize: 12,
              color: message.refusal ? 'var(--status-danger)' : 'var(--neutral-700)',
            }}
          >
            {message.refusal ? 'Refused: ' : ''}
            {message.text}
          </p>
        )}
      </Card>

      <Card title="Decided" sub="A rejection returns the exception to its queue; it does not close it.">
        {decided.length === 0 ? (
          <Empty>Nothing decided yet.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Approval</th>
                <th>Loan</th>
                <th>Proposed</th>
                <th>By</th>
                <th>Outcome</th>
                <th>Decided by</th>
              </tr>
            </thead>
            <tbody>
              {decided.map((a) => (
                <tr key={a.id}>
                  <td className="mono-sm">{a.id}</td>
                  <td className="mono-sm">{a.loan_id}</td>
                  <td>{a.proposed_action}</td>
                  <td>{a.proposed_by}</td>
                  <td>
                    <Badge tone={a.status === 'approved' ? 'success' : 'danger'}>{a.status}</Badge>
                  </td>
                  <td>{a.decided_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
