import { useState } from 'react'
import { api } from '../api'
import { useAsync } from '../hooks'
import { Badge, Card, Empty } from '../ui'

/**
 * The audit trail, with its integrity check on screen.
 *
 * "We log everything" is a claim. `chain_intact` is something a compliance
 * reviewer can watch you verify, in front of them, in one request — the server
 * re-walks every row and rehashes it on each call rather than reporting a
 * cached flag.
 */
export default function Audit({ refreshKey }: { refreshKey: number }) {
  const [kind, setKind] = useState('')
  const [caseId, setCaseId] = useState('')
  const { data, error } = useAsync(
    () => api.audit({ kind: kind || undefined, case_id: caseId || undefined }),
    [refreshKey, kind, caseId],
  )

  if (error) return <Empty>{error}</Empty>
  if (!data) return <Empty>Loading…</Empty>

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Audit Trail</h1>
          <p>Append-only and hash-chained. Every row names an actor and whether it was AI or a person.</p>
        </div>
        <div className="chip-row">
          <input
            type="text"
            style={{ width: 180 }}
            placeholder="Filter by loan id"
            value={caseId}
            onChange={(e) => setCaseId(e.target.value)}
          />
          <select style={{ width: 150 }} value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">AI and human</option>
            <option value="ai">AI only</option>
            <option value="human">Human only</option>
            <option value="system">System only</option>
          </select>
        </div>
      </div>

      <Card style={{ marginBottom: 14 }}>
        <div className="chip-row" style={{ justifyContent: 'space-between' }}>
          <div>
            <strong>Chain integrity</strong>
            <div className="hint">
              Every row stores sha256(previous hash + its own payload). Altering or deleting
              any historical row breaks every hash after it.
            </div>
          </div>
          {data.chain_intact ? (
            <Badge tone="success">Verified intact</Badge>
          ) : (
            <Badge tone="danger">Broken at {data.first_broken_hash?.slice(0, 12)}…</Badge>
          )}
        </div>
      </Card>

      <Card title={`${data.entries.length} most recent entries`}>
        {data.entries.length === 0 ? (
          <Empty>Nothing recorded yet.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Actor</th>
                <th>Kind</th>
                <th>Action</th>
                <th>Case</th>
                <th>Detail</th>
                <th>Hash</th>
              </tr>
            </thead>
            <tbody>
              {data.entries.map((e) => (
                <tr key={e.id}>
                  <td className="mono-sm">{e.at.replace('T', ' ').slice(0, 19)}</td>
                  <td>
                    {e.actor}
                    <div className="hint">{e.role}</div>
                  </td>
                  <td>
                    <Badge tone={e.kind === 'human' ? 'navy' : e.kind === 'ai' ? 'success' : 'muted'}>
                      {e.kind}
                    </Badge>
                  </td>
                  <td>{e.action}</td>
                  <td className="mono-sm">{e.case_id}</td>
                  <td className="mono-sm" style={{ maxWidth: 280, wordBreak: 'break-word' }}>
                    {Object.entries(e.detail)
                      .map(([k, v]) => `${k}=${String(v)}`)
                      .join(' · ')}
                  </td>
                  <td className="mono-sm">{e.hash.slice(0, 10)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
