import { api } from '../api'
import { useAsync } from '../hooks'
import { BarChart, CHART, Card, Empty, SplitBar, StatCard } from '../ui'

export default function Dashboard({
  refreshKey,
  onOpenLoan,
}: {
  refreshKey: number
  onOpenLoan: (id: string) => void
}) {
  const { data: k, error } = useAsync(() => api.kpis(), [refreshKey])
  const { data: runs } = useAsync(() => api.runs(), [refreshKey])

  if (error) return <Empty>{error}</Empty>
  if (!k) return <Empty>Loading…</Empty>

  const spend = (runs ?? []).reduce((s, r) => s + r.usd, 0)
  const cacheReads = (runs ?? []).reduce((s, r) => s + r.cache_read_tokens, 0)

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p>Portfolio state, derived live from the loan and exception rows.</p>
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 14 }}>
        <StatCard glyph="▤" label="Loans in book" value={k.loans} />
        <StatCard glyph="◎" label="Scanned by agents" value={k.scanned} tone="teal" />
        <StatCard glyph="⚑" label="Exceptions found" value={k.exceptions} tone="amber" />
        <StatCard glyph="⧗" label="Open with a human" value={k.open_hitl} tone="coral" />
        <StatCard glyph="✓" label="Auto-repaired" value={`${k.auto_repaired}/${k.auto_total}`} tone="teal" />
        <StatCard glyph="◇" label="Straight-through" value={`${k.stp_pct}%`} tone="teal" />
        <StatCard glyph="▲" label="Ready for underwriting" value={k.ready} />
        <StatCard glyph="⌁" label="Cycle time (days)" value={k.cycle_days} />
      </div>

      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 14 }}>
        <Card
          title="Disposition split"
          sub="Who handles the work the agents found — the auto lane versus a person."
        >
          <SplitBar
            segments={[
              { label: 'Auto-repair', value: k.auto_total, color: CHART.auto },
              { label: 'Human in the loop', value: k.exceptions - k.auto_total, color: CHART.hitl },
            ]}
          />
        </Card>

        <Card title="Exceptions by severity" sub="Severity is consequence, not certainty.">
          {k.exceptions === 0 ? (
            <Empty>Nothing found yet. Run a scan.</Empty>
          ) : (
            <BarChart
              data={(['Critical', 'High', 'Medium', 'Low'] as const).map((s) => ({
                name: s,
                value: k.by_severity[s] ?? 0,
                color: CHART.severity[s],
              }))}
            />
          )}
        </Card>
      </div>

      <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', marginBottom: 14 }}>
        <Card title="Analyst queue load" sub="Open HITL cases waiting, by queue.">
          <BarChart
            hue={CHART.neutralHue}
            data={['A', 'B', 'C'].map((q) => ({
              name: `Queue ${q}`,
              value: k.queue_load[q] ?? 0,
            }))}
          />
        </Card>

        <Card title="Pipeline by stage" sub="Where the book sits in the origination funnel.">
          <BarChart
            hue={CHART.neutralHue}
            data={k.by_stage.map((s) => ({ name: s.name, value: s.loans }))}
          />
        </Card>
      </div>

      <Card
        title="Agent runs"
        sub={`${(runs ?? []).length} recorded · $${spend.toFixed(4)} spent · ${cacheReads.toLocaleString()} tokens read from cache`}
      >
        {!runs?.length ? (
          <Empty>No agent runs yet.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Agent</th>
                <th>Loan</th>
                <th>Status</th>
                <th style={{ textAlign: 'right' }}>Tools</th>
                <th style={{ textAlign: 'right' }}>Cache read</th>
                <th style={{ textAlign: 'right' }}>Cost</th>
              </tr>
            </thead>
            <tbody>
              {runs.slice(0, 12).map((r) => (
                <tr key={r.run_id} className="clickable" onClick={() => onOpenLoan(r.loan_id)}>
                  <td className="mono-sm">{r.run_id}</td>
                  <td>{r.agent}</td>
                  <td className="mono-sm">{r.loan_id}</td>
                  <td>{r.status}</td>
                  <td style={{ textAlign: 'right' }}>{r.tool_calls}</td>
                  <td style={{ textAlign: 'right' }} className="mono-sm">
                    {r.cache_read_tokens.toLocaleString()}
                  </td>
                  <td style={{ textAlign: 'right' }} className="mono-sm">
                    ${r.usd.toFixed(4)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
