import { useEffect, useMemo, useRef, useState } from 'react'

export interface Series { key: string; label: string; color: string }
type Row = { t: number } & Record<string, number | null>

const H = 170
const PAD = { l: 44, r: 10, t: 10, b: 22 }

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null)
  const [w, setW] = useState(480)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(220, Math.floor(e.contentRect.width))))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

function niceMax(v: number) {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  const n = v / p
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p
}

const clock = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

function Tooltip({ x, w, title, rows }: { x: number; w: number; title: string; rows: { label: string; color: string; value: string }[] }) {
  const left = Math.min(Math.max(x - 80, 0), w - 170)
  return (
    <div className="viz-tip" style={{ left }} role="status">
      <div className="viz-tip-title">{title}</div>
      {rows.map(r => (
        <div key={r.label} className="viz-tip-row">
          <span className="viz-key-line" style={{ background: r.color }} aria-hidden="true" />
          <b>{r.value}</b><span>{r.label}</span>
        </div>
      ))}
    </div>
  )
}

function Frame({ title, hint, series, legendShape, children, table }: {
  title: string; hint?: string; series: Series[]; legendShape: 'line' | 'rect'; children: React.ReactNode
  table: { head: string[]; rows: (string | number)[][] }
}) {
  const [showTable, setShowTable] = useState(false)
  return (
    <figure className="viz-card">
      <figcaption className="viz-head">
        <div>
          <h3>{title}</h3>
          {hint && <p className="viz-hint">{hint}</p>}
        </div>
        <button type="button" className="viz-table-btn" onClick={() => setShowTable(v => !v)} aria-pressed={showTable}>
          {showTable ? 'Chart' : 'Table'}
        </button>
      </figcaption>
      {series.length > 1 && (
        <ul className="viz-legend">
          {series.map(s => (
            <li key={s.key}><span className={legendShape === 'line' ? 'viz-key-line' : 'viz-key-rect'} style={{ background: s.color }} aria-hidden="true" />{s.label}</li>
          ))}
        </ul>
      )}
      {showTable ? (
        <div className="viz-table-wrap">
          <table className="viz-table">
            <thead><tr>{table.head.map(h => <th key={h}>{h}</th>)}</tr></thead>
            <tbody>{table.rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
          </table>
        </div>
      ) : children}
    </figure>
  )
}

function Axes({ w, max, fmt, points }: { w: number; max: number; fmt: (v: number) => string; points: Row[] }) {
  const ticks = [0, max / 2, max]
  const y = (v: number) => PAD.t + (H - PAD.t - PAD.b) * (1 - v / max)
  const xs = points.length > 1 ? [0, Math.floor((points.length - 1) / 2), points.length - 1] : [0]
  const x = (i: number) => PAD.l + ((w - PAD.l - PAD.r) * i) / Math.max(points.length - 1, 1)
  return (
    <g className="viz-axes" aria-hidden="true">
      {ticks.map(t => (
        <g key={t}>
          <line x1={PAD.l} x2={w - PAD.r} y1={y(t)} y2={y(t)} className={t === 0 ? 'viz-base' : 'viz-grid'} />
          <text x={PAD.l - 6} y={y(t) + 4} textAnchor="end">{fmt(t)}</text>
        </g>
      ))}
      {xs.map(i => points[i] && (
        <text key={i} x={x(i)} y={H - 6} textAnchor={i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}>{clock(points[i].t)}</text>
      ))}
    </g>
  )
}

/** Lines over time with a crosshair that snaps to the nearest bucket. Null values leave a gap. */
export function LineChart({ title, hint, points, series, fmt }: { title: string; hint?: string; points: Row[]; series: Series[]; fmt: (v: number) => string }) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const max = useMemo(() => niceMax(Math.max(0, ...points.flatMap(p => series.map(s => p[s.key] ?? 0)))), [points, series])
  const x = (i: number) => PAD.l + ((w - PAD.l - PAD.r) * i) / Math.max(points.length - 1, 1)
  const y = (v: number) => PAD.t + (H - PAD.t - PAD.b) * (1 - v / max)
  const path = (key: string) => {
    let d = '', pen = false
    points.forEach((p, i) => {
      const v = p[key]
      if (v == null) { pen = false; return }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`
      pen = true
    })
    return d
  }
  const pick = (clientX: number, el: Element) => {
    const r = el.getBoundingClientRect()
    const i = Math.round(((clientX - r.left - PAD.l) / (w - PAD.l - PAD.r)) * (points.length - 1))
    setHover(Math.min(points.length - 1, Math.max(0, i)))
  }
  const sparse = points.filter(p => series.some(s => (p[s.key] ?? 0) > 0)).length <= 40
  const singles = points.map((p, i) => series.map(s => ({ s, v: p[s.key], i, prevNull: i === 0 || p[s.key] != null && points[i - 1][s.key] == null,
    nextNull: i === points.length - 1 || points[i + 1][s.key] == null })))
  return (
    <Frame title={title} hint={hint} series={series} legendShape="line"
      table={{ head: ['Time', ...series.map(s => s.label)], rows: points.map(p => [clock(p.t), ...series.map(s => (p[s.key] == null ? '—' : fmt(p[s.key] as number)))]) }}>
      <div ref={ref} className="viz-plot">
        <svg width={w} height={H} role="img" aria-label={`${title}, ${points.length} points`} tabIndex={0}
          onPointerMove={e => pick(e.clientX, e.currentTarget)} onPointerLeave={() => setHover(null)}
          onKeyDown={e => {
            if (e.key === 'ArrowRight') setHover(h => Math.min(points.length - 1, (h ?? -1) + 1))
            if (e.key === 'ArrowLeft') setHover(h => Math.max(0, (h ?? points.length) - 1))
            if (e.key === 'Escape') setHover(null)
          }} onBlur={() => setHover(null)}>
          <Axes w={w} max={max} fmt={fmt} points={points} />
          {series.map(s => <path key={s.key} d={path(s.key)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />)}
          {/* markers when data is sparse (and always for isolated points, which a line can't show) */}
          {singles.flat().filter(p => p.v != null && ((sparse && p.v > 0) || (p.prevNull && p.nextNull))).map(p => (
            <circle key={`${p.s.key}-${p.i}`} cx={x(p.i)} cy={y(p.v as number)} r={4} fill={p.s.color} className="viz-ring" />
          ))}
          {hover != null && (
            <g aria-hidden="true">
              <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={H - PAD.b} className="viz-cross" />
              {series.map(s => points[hover][s.key] != null && (
                <circle key={s.key} cx={x(hover)} cy={y(points[hover][s.key] as number)} r={4.5} fill={s.color} className="viz-ring" />
              ))}
            </g>
          )}
        </svg>
        {hover != null && (
          <Tooltip x={x(hover)} w={w} title={clock(points[hover].t)}
            rows={series.map(s => ({ label: s.label, color: s.color, value: points[hover][s.key] == null ? 'no data' : fmt(points[hover][s.key] as number) }))} />
        )}
      </div>
    </Frame>
  )
}

/** Stacked bars per time bucket; the column is the hit target. */
export function StackedBars({ title, hint, points, series, fmt = v => String(v) }: {
  title: string; hint?: string; points: Row[]; series: Series[]; fmt?: (v: number) => string
}) {
  const [ref, w] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const max = useMemo(() => niceMax(Math.max(0, ...points.map(p => series.reduce((a, s) => a + (p[s.key] ?? 0), 0)))), [points, series])
  const inner = w - PAD.l - PAD.r
  const slot = inner / Math.max(points.length, 1)
  const bw = Math.max(2, Math.min(28, slot - 2))
  const y = (v: number) => PAD.t + (H - PAD.t - PAD.b) * (1 - v / max)
  const xs = (i: number) => PAD.l + slot * i + (slot - bw) / 2
  return (
    <Frame title={title} hint={hint} series={series} legendShape="rect"
      table={{ head: ['Time', ...series.map(s => s.label)], rows: points.map(p => [clock(p.t), ...series.map(s => fmt(p[s.key] ?? 0))]) }}>
      <div ref={ref} className="viz-plot">
        <svg width={w} height={H} role="img" aria-label={`${title}, ${points.length} time buckets`} tabIndex={0}
          onKeyDown={e => {
            if (e.key === 'ArrowRight') setHover(h => Math.min(points.length - 1, (h ?? -1) + 1))
            if (e.key === 'ArrowLeft') setHover(h => Math.max(0, (h ?? points.length) - 1))
            if (e.key === 'Escape') setHover(null)
          }} onBlur={() => setHover(null)} onPointerLeave={() => setHover(null)}>
          <Axes w={w} max={max} fmt={fmt} points={points} />
          {points.map((p, i) => {
            let base = 0
            const segs = series.map(s => ({ s, v: p[s.key] ?? 0 })).filter(x => x.v > 0)
            return (
              <g key={i} className={hover === i ? 'viz-hot' : ''}>
                {segs.map((seg, k) => {
                  const y0 = y(base), y1 = y(base + seg.v)
                  base += seg.v
                  const top = k === segs.length - 1
                  const h = Math.max(1, y0 - y1 - (top ? 0 : 2))   // 2px surface gap between stacked segments
                  const r = top ? Math.min(4, bw / 2, h) : 0
                  const x0 = xs(i), yTop = y0 - h
                  const d = `M${x0},${y0}V${yTop + r}Q${x0},${yTop} ${x0 + r},${yTop}H${x0 + bw - r}Q${x0 + bw},${yTop} ${x0 + bw},${yTop + r}V${y0}Z`
                  return <path key={seg.s.key} d={d} fill={seg.s.color} />
                })}
                <rect x={PAD.l + slot * i} y={PAD.t} width={slot} height={H - PAD.t - PAD.b} fill="transparent"
                  onPointerMove={() => setHover(i)} />
              </g>
            )
          })}
        </svg>
        {hover != null && points[hover] && (
          <Tooltip x={xs(hover) + bw / 2} w={w} title={clock(points[hover].t)}
            rows={series.map(s => ({ label: s.label, color: s.color, value: fmt(points[hover][s.key] ?? 0) }))} />
        )}
      </div>
    </Frame>
  )
}
