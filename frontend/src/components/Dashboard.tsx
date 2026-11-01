import { useEffect, useMemo, useRef, useState } from 'react'
import type { Alerts, Limits, MetricPoint, TranscriptFile } from '../api'
import { AdminAuthError, adminApi, adminToken, opsAlerts, opsTimeseries, runSelfTest } from '../api'
import { OpsPanel } from './OpsPanel'

// ---------------------------------------------------------------------------------------------
// Chart: one measure over the last 10 minutes, area + line, crosshair tooltip.
// ---------------------------------------------------------------------------------------------
const W = 320, H = 120, PAD_L = 34, PAD_R = 8, PAD_T = 8, PAD_B = 18

function niceMax(v: number) {
  if (v <= 0) return 1
  const p = 10 ** Math.floor(Math.log10(v))
  const n = v / p
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p
}

const fmtNum = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : v % 1 ? v.toFixed(1) : String(v))

interface ChartProps {
  title: string
  hint: string
  points: MetricPoint[]
  value: (p: MetricPoint) => number | null
  unit?: string
  windowS: number
  alarm?: (latest: number | null) => boolean
}

function Chart({ title, hint, points, value, unit = '', windowS, alarm }: ChartProps) {
  const [hover, setHover] = useState<number | null>(null)
  const ref = useRef<SVGSVGElement>(null)
  const now = points.length ? points[points.length - 1].ts : Date.now() / 1000
  const t0 = now - windowS
  const vals = points.map(value)
  const max = niceMax(Math.max(0, ...vals.filter((v): v is number => v != null)))
  const x = (ts: number) => PAD_L + ((ts - t0) / windowS) * (W - PAD_L - PAD_R)
  const y = (v: number) => PAD_T + (1 - v / max) * (H - PAD_T - PAD_B)

  // Break the line where data is missing instead of drawing through the gap.
  const segments: string[] = []
  let cur: string[] = []
  points.forEach((p, i) => {
    const v = vals[i]
    if (v == null) { if (cur.length) segments.push(cur.join(' ')); cur = []; return }
    cur.push(`${x(p.ts).toFixed(1)},${y(v).toFixed(1)}`)
  })
  if (cur.length) segments.push(cur.join(' '))

  const latest = [...vals].reverse().find(v => v != null) ?? null
  const hovered = hover != null ? points[hover] : null
  const hv = hover != null ? vals[hover] : null
  const alarming = alarm?.(latest) ?? false

  const onMove = (e: React.PointerEvent) => {
    const rect = ref.current?.getBoundingClientRect()
    if (!rect || !points.length) return
    const px = ((e.clientX - rect.left) / rect.width) * W
    const ts = t0 + ((px - PAD_L) / (W - PAD_L - PAD_R)) * windowS
    let best = 0
    points.forEach((p, i) => { if (Math.abs(p.ts - ts) < Math.abs(points[best].ts - ts)) best = i })
    setHover(best)
  }

  return (
    <figure className={`chart ${alarming ? 'chart-alarm' : ''}`}>
      <figcaption>
        <span className="chart-title">{title}</span>
        <span className="chart-latest">
          {alarming && <span className="alarm-dot" aria-label="needs attention" />}
          {latest == null ? '—' : `${fmtNum(latest)}${unit}`}
        </span>
      </figcaption>
      <p className="chart-hint">{hint}</p>
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, latest ${latest ?? 'no data'}${unit}`}
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {[0, 0.5, 1].map(f => (
          <g key={f}>
            <line className="grid" x1={PAD_L} x2={W - PAD_R} y1={y(max * f)} y2={y(max * f)} />
            <text className="axis" x={PAD_L - 5} y={y(max * f) + 3} textAnchor="end">{fmtNum(max * f)}</text>
          </g>
        ))}
        {[windowS, windowS / 2, 0].map(s => (
          <text key={s} className="axis" x={x(now - s)} y={H - 4} textAnchor={s === windowS ? 'start' : s === 0 ? 'end' : 'middle'}>
            {s === 0 ? 'now' : `-${Math.round(s / 60)}m`}
          </text>
        ))}
        {segments.map((seg, i) => {
          const pts = seg.split(' ')
          const first = pts[0].split(',')[0], last = pts[pts.length - 1].split(',')[0]
          return (
            <g key={i}>
              <polygon className="area" points={`${first},${y(0)} ${seg} ${last},${y(0)}`} />
              <polyline className="line" points={seg} />
            </g>
          )
        })}
        {latest != null && points.length > 0 && vals[vals.length - 1] != null && (
          <circle className="endpoint" cx={x(points[points.length - 1].ts)} cy={y(vals[vals.length - 1]!)} r={3} />
        )}
        {hovered && (
          <g>
            <line className="crosshair" x1={x(hovered.ts)} x2={x(hovered.ts)} y1={PAD_T} y2={H - PAD_B} />
            {hv != null && <circle className="hover-dot" cx={x(hovered.ts)} cy={y(hv)} r={4} />}
          </g>
        )}
        <rect x={PAD_L} y={0} width={W - PAD_L - PAD_R} height={H} fill="transparent" />
      </svg>
      {hovered && (
        <div className="tip" style={{ left: `${(x(hovered.ts) / W) * 100}%` }}>
          <b>{hv == null ? 'no data' : `${fmtNum(hv)}${unit}`}</b>
          <span>{new Date(hovered.ts * 1000).toLocaleTimeString()}</span>
        </div>
      )}
    </figure>
  )
}

// ---------------------------------------------------------------------------------------------
// Test lab
// ---------------------------------------------------------------------------------------------
interface TestRow { i: number; status: number; path: string | null; scope: string | null; retry_after: string | null; ms: number }
interface TestRun { running: boolean; steps: string[]; rows: TestRow[]; verdict?: { passed: boolean; summary: string }; error?: string }

const TESTS = [
  {
    name: 'rate_limit', title: 'Rate limiting (per fan)',
    what: 'One fan asks faster than allowed. Each fan has a token bucket: a burst allowance that refills at a steady rate.',
    expect: 'The first burst of requests succeeds, the rest get HTTP 429 with a Retry-After header. Other fans are unaffected.',
  },
  {
    name: 'throttle', title: 'Throttling (global)',
    what: 'Many fans together exceed the total capacity. The global bucket caps requests per second across everyone.',
    expect: 'The global limit is lowered to 1/s for the test, then restored. Almost every request gets 429 with scope "global".',
  },
  {
    name: 'load_shed', title: 'Load shedding (LLM queue)',
    what: 'More AI questions arrive than the model can serve. The admission controller allows 1 running and a bounded queue.',
    expect: 'One or two get full AI answers. The rest are shed: answered from data in about a second instead of waiting for the model.',
  },
] as const

function pathLabel(r: TestRow) {
  if (r.status === 429) return `429 · ${r.scope}`
  if (r.status !== 200) return `${r.status || 'network'} error`
  return r.path ?? '200'
}

function TestLab({ unlocked }: { unlocked: boolean }) {
  const [runs, setRuns] = useState<Record<string, TestRun>>({})
  const busy = Object.values(runs).some(r => r.running)

  const start = async (name: string) => {
    setRuns(r => ({ ...r, [name]: { running: true, steps: [], rows: [] } }))
    const upd = (fn: (t: TestRun) => TestRun) => setRuns(r => ({ ...r, [name]: fn(r[name]) }))
    try {
      for await (const ev of runSelfTest(name)) {
        if (ev.type === 'step') upd(t => ({ ...t, steps: [...t.steps, ev.text as string] }))
        else if (ev.type === 'request') upd(t => ({ ...t, rows: [...t.rows, ev as unknown as TestRow] }))
        else if (ev.type === 'verdict') upd(t => ({ ...t, verdict: { passed: ev.passed as boolean, summary: ev.summary as string } }))
      }
    } catch (e) {
      if (e instanceof AdminAuthError) adminToken.clear()   // the panel on the right shows the unlock form again
      upd(t => ({ ...t, error: e instanceof AdminAuthError ? 'Admin token rejected: unlock again in the panel on the right.' : (e as Error).message }))
    } finally {
      upd(t => ({ ...t, running: false }))
    }
  }

  return (
    <section className="lab" aria-label="Traffic tests">
      <h2>Traffic test lab</h2>
      <p className="dim">Each test sends real requests to this server through the same limits fans hit, then checks the result.
        Running a test also rate-limits your own browser for a few seconds, because it shares your IP.</p>
      {!unlocked && <p className="notice">Unlock with the admin token in the panel on the right to run tests.</p>}
      <div className="lab-grid">
        {TESTS.map(t => {
          const run = runs[t.name]
          return (
            <article key={t.name} className="lab-card">
              <header>
                <h3>{t.title}</h3>
                {run?.verdict && <span className={`verdict ${run.verdict.passed ? 'pass' : 'fail'}`}>{run.verdict.passed ? '✓ PASS' : '✕ FAIL'}</span>}
              </header>
              <p><b>Tests:</b> {t.what}</p>
              <p><b>Expect:</b> {t.expect}</p>
              <button type="button" className="run" disabled={!unlocked || busy} onClick={() => start(t.name)}>
                {run?.running ? 'Running…' : run ? 'Run again' : 'Run test'}
              </button>
              {run && (
                <div className="lab-out">
                  {run.steps.map((s, i) => <p key={i} className="step">{s}</p>)}
                  {run.rows.length > 0 && (
                    <div className="lab-table">
                      <table>
                        <thead><tr><th>#</th><th>Result</th><th className="n">ms</th></tr></thead>
                        <tbody>
                          {run.rows.map(r => (
                            <tr key={r.i} className={r.status === 429 ? 'r-limited' : r.path === 'shed' ? 'r-shed' : r.status !== 200 ? 'r-err' : ''}>
                              <td>{r.i}</td><td>{pathLabel(r)}{r.retry_after ? ` · retry ${r.retry_after}s` : ''}</td><td className="n">{r.ms}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {run.verdict && <p className="summary">{run.verdict.summary}</p>}
                  {run.error && <p className="msg-error">{run.error}</p>}
                </div>
              )}
            </article>
          )
        })}
      </div>
    </section>
  )
}

function LimitsEditor({ unlocked }: { unlocked: boolean }) {
  const [lim, setLim] = useState<Limits | null>(null)
  const [defaults, setDefaults] = useState<Limits | null>(null)
  const [msg, setMsg] = useState('')
  useEffect(() => {
    if (!unlocked) return
    adminApi.limits().then(r => { setLim(r.current); setDefaults(r.defaults) }).catch(e => setMsg(e.message))
  }, [unlocked])
  if (!unlocked || !lim) return null
  const fields: [keyof Limits, string][] = [
    ['rate_per_client_per_min', 'Per fan, per minute'], ['rate_burst_per_client', 'Per fan burst'],
    ['rate_global_per_s', 'Global, per second'], ['llm_queue_max', 'LLM queue places'], ['llm_queue_timeout_s', 'Queue wait limit (s)'],
  ]
  const save = async () => {
    try { setLim((await adminApi.setLimits(lim)).current); setMsg('Saved. New limits apply now.') } catch (e) { setMsg((e as Error).message) }
  }
  const reset = async () => {
    try { setLim((await adminApi.resetLimits()).current); setMsg('Back to defaults.') } catch (e) { setMsg((e as Error).message) }
  }
  return (
    <section className="limits">
      <h2>Live limits</h2>
      <p className="dim">Change limits without a restart, for example to throttle harder during an incident.</p>
      <div className="limits-grid">
        {fields.map(([k, label]) => (
          <label key={k} htmlFor={`lim-${k}`}>
            <span>{label}</span>
            <input id={`lim-${k}`} type="number" min={0} step="any" value={lim[k]}
              onChange={e => setLim({ ...lim, [k]: Number(e.target.value) })} />
            {defaults && defaults[k] !== lim[k] && <span className="dim small">default {defaults[k]}</span>}
          </label>
        ))}
      </div>
      <div className="row">
        <button type="button" className="mini" onClick={save}>Apply</button>
        <button type="button" className="mini" onClick={reset}>Reset to defaults</button>
        {msg && <span className="dim small">{msg}</span>}
      </div>
    </section>
  )
}

function ConversationLogs({ unlocked }: { unlocked: boolean }) {
  const [files, setFiles] = useState<TranscriptFile[]>([])
  const [open, setOpen] = useState<{ name: string; content: string } | null>(null)
  const [msg, setMsg] = useState('')
  const load = () => adminApi.transcripts().then(r => { setFiles(r.files); setMsg('') }).catch(e => setMsg((e as Error).message))
  useEffect(() => { if (unlocked) load() }, [unlocked])
  if (!unlocked) return null
  const view = async (name: string) => {
    try { setOpen(await adminApi.transcript(name)) } catch (e) { setMsg((e as Error).message) }
  }
  return (
    <section className="logs" aria-label="Conversation logs">
      <div className="row between">
        <div>
          <h2>Conversation logs</h2>
          <p className="dim">One .txt per chat session in <span className="mono">backend/var/conversations/</span>: every question, the router's
            decision, each agent, every tool call with its arguments and result, and the answer. Questions are PII-redacted.</p>
        </div>
        <button type="button" className="mini" onClick={load}>Refresh</button>
      </div>
      {msg && <p className="msg-error">{msg}</p>}
      {!files.length ? <p className="dim">No conversations yet. Ask something in the Match tab.</p> : (
        <div className="logs-grid">
          <ul className="logs-list">
            {files.map(f => (
              <li key={f.name}>
                <button type="button" className={`log-file ${open?.name === f.name ? 'on' : ''}`} onClick={() => view(f.name)}>
                  <span className="mono">{f.name}</span>
                  <span className="dim small">{f.turns} turn{f.turns === 1 ? '' : 's'} · {(f.bytes / 1024).toFixed(1)} KB · {new Date(f.modified * 1000).toLocaleTimeString()}</span>
                </button>
              </li>
            ))}
          </ul>
          <pre className="transcript">{open ? open.content : 'Pick a file to read it.'}</pre>
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------------------------
export function Dashboard() {
  const [points, setPoints] = useState<MetricPoint[]>([])
  const [alerts, setAlerts] = useState<Alerts | null>(null)
  const [down, setDown] = useState(false)
  const [unlocked, setUnlocked] = useState(() => !!adminToken.get())
  const windowS = 600

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [ts, al] = await Promise.all([opsTimeseries(10), opsAlerts()])
        if (!alive) return
        setPoints(ts.points); setAlerts(al); setDown(false)
      } catch { if (alive) setDown(true) }
    }
    tick()
    const t = window.setInterval(tick, 2000)
    // The ops panel stores the admin token; notice when it does.
    const u = window.setInterval(() => setUnlocked(!!adminToken.get()), 1000)
    return () => { alive = false; window.clearInterval(t); window.clearInterval(u) }
  }, [])

  const overall = down ? 'down' : alerts?.overall ?? 'healthy'
  const headline = down ? 'API unreachable' : overall === 'healthy' ? 'All systems normal' : overall === 'degraded' ? 'Degraded' : 'Something is down'
  const charts = useMemo(() => [
    { title: 'Answers', hint: 'per minute (30 s average), all paths', value: (p: MetricPoint) => p.answers_per_min },
    { title: 'AI answers', hint: 'per minute, reached the LLM', value: (p: MetricPoint) => p.llm_per_min },
    { title: 'Rate limited', hint: '429s per minute, per-fan and per-IP', value: (p: MetricPoint) => p.rate_limited_per_min, alarm: (v: number | null) => !!v },
    { title: 'Throttled', hint: '429s per minute, global limit', value: (p: MetricPoint) => p.throttled_per_min, alarm: (v: number | null) => !!v },
    { title: 'Shed', hint: 'questions per minute sent to the fast path', value: (p: MetricPoint) => p.shed_per_min, alarm: (v: number | null) => !!v },
    { title: 'Server errors', hint: '5xx per minute', value: (p: MetricPoint) => p.errors_per_min, alarm: (v: number | null) => !!v },
    { title: 'LLM queue', hint: 'questions waiting for the model', value: (p: MetricPoint) => p.llm_queue, alarm: (v: number | null) => (v ?? 0) > 0 },
    { title: 'Answer time p95', hint: 'seconds, last 60 s', value: (p: MetricPoint) => (p.latency_p95_ms == null ? null : Math.round(p.latency_p95_ms / 100) / 10), unit: 's', alarm: (v: number | null) => (v ?? 0) > 30 },
    { title: 'Feed lag', hint: 'seconds since the last ball', value: (p: MetricPoint) => p.feed_lag_s, unit: 's', alarm: (v: number | null) => (v ?? 0) > 20 },
    { title: 'Cache hit rate', hint: '% of cacheable questions', value: (p: MetricPoint) => p.cache_hit_pct, unit: '%' },
    { title: 'Components down', hint: 'of 5 monitored', value: (p: MetricPoint) => p.components_down, alarm: (v: number | null) => (v ?? 0) > 0 },
    { title: 'Service level', hint: '0 normal · 1 busy · 2 AI offline', value: (p: MetricPoint) => p.degrade_level, alarm: (v: number | null) => (v ?? 0) > 0 },
  ], [])

  return (
    <div className="dash">
      <div className="dash-main">
        <section className={`status status-${overall}`} aria-live="polite">
          <span className="status-icon" aria-hidden="true">{overall === 'healthy' ? '✓' : overall === 'degraded' ? '!' : '✕'}</span>
          <div>
            <h2>{headline}</h2>
            <p>{alerts?.active.length ? `${alerts.active.length} active alert${alerts.active.length > 1 ? 's' : ''}` : 'No active alerts'} · updated every 2 s</p>
          </div>
        </section>

        {alerts && alerts.active.length > 0 && (
          <ul className="alerts">
            {alerts.active.map(a => (
              <li key={a.key} className={`alert sev-${a.severity}`}>
                <span className="sev">{a.severity}</span>
                <div><b>{a.title}</b><span className="dim"> · {a.detail}</span></div>
                <span className="mono dim">for {Math.max(0, Math.round(Date.now() / 1000 - a.since))}s</span>
              </li>
            ))}
          </ul>
        )}

        <section className="charts" aria-label="Metrics, last 10 minutes">
          {charts.map(c => <Chart key={c.title} {...c} points={points} windowS={windowS} />)}
        </section>

        <TestLab unlocked={unlocked} />
        <LimitsEditor unlocked={unlocked} />
        <ConversationLogs unlocked={unlocked} />

        <section className="history">
          <h2>Alert history</h2>
          {!alerts?.history.length ? <p className="dim">Nothing has fired yet. Use “Break” on a component or run a test to see alerts here.</p> : (
            <ol>
              {alerts.history.map((h, i) => (
                <li key={i} className={`ev-${h.event}`}>
                  <span className="mono dim">{new Date(h.ts * 1000).toLocaleTimeString()}</span>
                  <span className={`chip-ev ${h.event}`}>{h.event}</span>
                  <b>{h.title}</b> <span className="dim">{h.detail}</span>
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>
      <OpsPanel />
    </div>
  )
}
