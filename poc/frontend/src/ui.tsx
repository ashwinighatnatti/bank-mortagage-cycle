/**
 * Shared primitives and the four dashboard charts.
 *
 * CHART DECISIONS, MADE IN THIS ORDER: form first, colour last.
 *
 * Every chart here answers a *magnitude across a few named categories*
 * question, so every one of them is a horizontal bar. The reference design
 * showed a line chart and an area chart as well; both are absent here on
 * purpose, because this system has no time series. Drawing a trend would mean
 * inventing history, and a fabricated line on a governance dashboard is worse
 * than a missing one.
 *
 * Colour: queue load and the stage funnel use ONE hue. Identity is carried by
 * the row label, not the fill, so a categorical palette would add a
 * colour-vision hazard for nothing — the first trio I tried (navy/teal/coral)
 * failed the validator at ΔE 6.5 for protanopia. The lane split is genuinely
 * categorical (auto vs HITL) and uses teal/amber, which passes; its amber sits
 * below 3:1 on the surface, so both segments carry a direct label.
 *
 * Severity uses the reserved status palette, which is legitimate because
 * severity *is* a status — and it ships with a text label, never colour alone.
 */

import type { ReactNode } from 'react'

/* ---------- primitives ---------- */
export function Card({
  title,
  sub,
  children,
  style,
}: {
  title?: string
  sub?: string
  children: ReactNode
  style?: React.CSSProperties
}) {
  return (
    <section className="card" style={style}>
      {title && <h3>{title}</h3>}
      {sub && <div className="sub">{sub}</div>}
      {children}
    </section>
  )
}

export function StatCard({
  value,
  label,
  glyph,
  tone = 'navy',
}: {
  value: ReactNode
  label: string
  glyph: string
  tone?: 'navy' | 'teal' | 'coral' | 'amber'
}) {
  const bg = {
    navy: 'var(--navy-50)',
    teal: 'var(--teal-50)',
    coral: '#FDEDE9',
    amber: '#FEF1E6',
  }[tone]
  const fg = {
    navy: 'var(--navy-500)',
    teal: 'var(--teal-500)',
    coral: 'var(--coral-600)',
    amber: '#B4610F',
  }[tone]
  return (
    <div className="card stat">
      <div className="glyph" style={{ background: bg, color: fg }} aria-hidden>
        {glyph}
      </div>
      <div className="value">{value}</div>
      <div className="label">{label}</div>
    </div>
  )
}

export function Badge({
  children,
  tone = 'muted',
}: {
  children: ReactNode
  tone?: 'danger' | 'warning' | 'success' | 'muted' | 'navy'
}) {
  return <span className={`badge ${tone}`}>{children}</span>
}

export const severityTone = (s: string) =>
  s === 'Critical' || s === 'High' ? 'danger' : s === 'Medium' ? 'warning' : 'navy'

export const laneTone = (lane: string) => (lane === 'auto' ? 'success' : 'warning')

export function SeverityBadge({ severity }: { severity: string }) {
  // Text plus colour, never colour alone.
  return <Badge tone={severityTone(severity)}>{severity}</Badge>
}

export function LaneBadge({ lane, requiresSup }: { lane: string; requiresSup?: boolean }) {
  return (
    <span className="chip-row">
      <Badge tone={laneTone(lane)}>{lane === 'auto' ? 'Auto-repair' : 'HITL'}</Badge>
      {requiresSup && <Badge tone="danger">Sign-off</Badge>}
    </span>
  )
}

export function Avatar({ name }: { name: string }) {
  const initials = name
    .split(' ')
    .map((p) => p[0])
    .slice(0, 2)
    .join('')
  return <div className="avatar">{initials}</div>
}

export function ProgressBar({ pct, color = 'var(--teal-500)' }: { pct: number; color?: string }) {
  return (
    <div className="bar-track">
      <div className="bar-fill" style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: color }} />
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

/* ---------- charts ---------- */
export interface BarDatum {
  name: string
  value: number
  color?: string
  note?: string
}

/**
 * Magnitude across a few named categories. One hue unless a caller passes an
 * explicit colour, which only the severity chart does (status palette).
 */
export function BarChart({
  data,
  hue = 'var(--navy-500)',
  max,
}: {
  data: BarDatum[]
  hue?: string
  max?: number
}) {
  const top = Math.max(1, max ?? Math.max(...data.map((d) => d.value), 1))
  return (
    <div role="img" aria-label={data.map((d) => `${d.name}: ${d.value}`).join(', ')}>
      {data.map((d) => (
        <div className="chart-row" key={d.name} title={d.note ?? `${d.name}: ${d.value}`}>
          <span className="name">{d.name}</span>
          <span className="track">
            <span
              className="fill"
              style={{ width: `${(d.value / top) * 100}%`, background: d.color ?? hue }}
            />
          </span>
          <span className="val">{d.value}</span>
        </div>
      ))}
    </div>
  )
}

/**
 * A two-category part-to-whole. A donut was the reference's choice; a single
 * stacked bar reads faster at two slices and leaves room for direct labels,
 * which the amber needs — it sits below 3:1 against the card surface.
 */
export function SplitBar({
  segments,
}: {
  segments: { label: string; value: number; color: string }[]
}) {
  const total = segments.reduce((s, x) => s + x.value, 0)
  if (total === 0) return <Empty>Nothing dispositioned yet.</Empty>
  return (
    <>
      <div className="split-bar">
        {segments.map((s) => (
          <span
            key={s.label}
            style={{ width: `${(s.value / total) * 100}%`, background: s.color }}
            title={`${s.label}: ${s.value}`}
          >
            {s.value > 0 ? s.value : ''}
          </span>
        ))}
      </div>
      <div className="legend">
        {segments.map((s) => (
          <span key={s.label}>
            <i style={{ background: s.color }} />
            {s.label} — {s.value} ({total ? Math.round((s.value / total) * 100) : 0}%)
          </span>
        ))}
      </div>
    </>
  )
}

export const CHART = {
  auto: 'var(--teal-500)',
  hitl: 'var(--amber-500)',
  neutralHue: 'var(--navy-500)',
  severity: {
    Critical: 'var(--status-danger)',
    High: 'var(--status-danger)',
    Medium: 'var(--amber-500)',
    Low: 'var(--navy-300)',
  } as Record<string, string>,
}
