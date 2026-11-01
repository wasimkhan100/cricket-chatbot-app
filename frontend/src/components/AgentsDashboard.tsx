import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AgentRun, AgentSummary, CsvFile, ErrorGroup, Health, LogLine, Range, ToolStat, TsPoint } from '../api'
import { AdminAuthError, adminApi, adminToken, downloadCsv } from '../api'
import type { Trace } from '../types'
import { LineChart, StackedBars } from './obs/Charts'
import { TraceView } from './OpsPanel'

const RANGES: { id: Range; label: string }[] = [{ id: '15m', label: '15 min' }, { id: '1h', label: '1 hour' }, { id: '6h', label: '6 hours' }, { id: '24h', label: '24 hours' }]
const REFRESH = [{ s: 0, label: 'Off' }, { s: 5, label: '5 s' }, { s: 15, label: '15 s' }, { s: 30, label: '30 s' }]
const STATUSES = ['', 'ok', 'error', 'degraded', 'blocked', 'cancelled']
const S1 = 'var(--viz-1)', S2 = 'var(--viz-2)', GOOD = 'var(--st-good)', WARN = 'var(--st-warning)', CRIT = 'var(--st-critical)'

const fmtMs = (ms: number | null | undefined) => (ms == null ? '—' : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`)
const fmtNum = (n: number) => n.toLocaleString()
const fmtCost = (n: number) => (n === 0 ? '$0.00' : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`)
const clock = (t: number) => new Date(t * 1000).toLocaleTimeString()
const ago = (t: number, now: number) => { const s = Math.max(0, Math.round(now - t)); return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago` }

// --------------------------------------------------------------------------- small pieces
function StatusPill({ s }: { s: string }) {
  const icon = s === 'ok' ? '✓' : s === 'error' ? '✕' : s === 'running' ? '…' : '!'
  return <span className={`pill st-${s}`}><span aria-hidden="true">{icon}</span> {s}</span>
}

const HEALTH: Record<Health, { icon: string; cls: string }> = {
  healthy: { icon: '✓', cls: 'st-ok' }, degraded: { icon: '!', cls: 'st-degraded' }, failing: { icon: '✕', cls: 'st-error' }, idle: { icon: '–', cls: 'st-idle' },
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'good' | 'bad' | 'warn' }) {
  return (
    <div className={`kpi ${tone ? `kpi-${tone}` : ''}`}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

function Panel({ title, sub, actions, children, className = '' }: { title: string; sub?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`obs-panel ${className}`}>
      <header className="obs-panel-head">
        <div><h2>{title}</h2>{sub && <p className="dim small">{sub}</p>}</div>
        {actions && <div className="row">{actions}</div>}
      </header>
      {children}
    </section>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="obs-empty">{children}</div>
}

// --------------------------------------------------------------------------- unlock
function Unlock({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState('')
  const [msg, setMsg] = useState('')
  const submit = async () => {
    const t = adminToken.normalize(value)
    if (!t) return
    if (await adminToken.verify(t)) { adminToken.set(t); onDone() } else setMsg("That token doesn't match CRICKET_ADMIN_TOKEN in backend/.env.")
  }
  return (
    <main className="obs">
      <Panel title="Agent observability" sub="Logs, metrics and traces for every agent run. Unlock with the admin token from backend/.env.">
        <form className="row" onSubmit={e => { e.preventDefault(); submit() }}>
          <label className="sr-only" htmlFor="obs-token">Admin token</label>
          <input id="obs-token" className="obs-input" type="password" value={value} onChange={e => setValue(e.target.value)} placeholder="Admin token" autoComplete="off" />
          <button type="submit" className="mini">Unlock</button>
          {msg && <span className="msg-error small">{msg}</span>}
        </form>
      </Panel>
    </main>
  )
}

// --------------------------------------------------------------------------- handoff graph
const LAYERS = [['Crickbot'], ['Planner'], ['Router'], ['Live', 'Stats analyst', 'Rules expert'], ['Commentator']]

function HandoffGraph({ summary, onPick }: { summary: AgentSummary; onPick: (agent: string) => void }) {
  const known = new Set(LAYERS.flat())
  const extra = summary.agents.map(a => a.agent).filter(a => !known.has(a))
  const layers = LAYERS.map((l, i) => (i === 3 ? [...l, ...extra] : l))
  const W = 760, H = 230, colW = W / layers.length
  const pos: Record<string, { x: number; y: number }> = {}
  layers.forEach((col, i) => col.forEach((a, j) => { pos[a] = { x: colW * i + colW / 2, y: (H / (col.length + 1)) * (j + 1) } }))
  const stats = Object.fromEntries(summary.agents.map(a => [a.agent, a]))
  const maxN = Math.max(1, ...summary.handoffs.map(h => h.count))
  if (!summary.handoffs.length) return <Empty>No handoffs in this range yet.</Empty>
  return (
    <div className="graph-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} className="handoff-graph" role="img" aria-label="Handoffs between agents">
        {summary.handoffs.map(h => {
          const a = pos[h.from], b = pos[h.to]
          if (!a || !b) return null
          const mx = (a.x + b.x) / 2
          const width = 1.5 + 5 * Math.sqrt(h.count / maxN)
          return (
            <g key={h.handoff}>
              <path d={`M${a.x + 52},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x - 52},${b.y}`} className="edge" strokeWidth={width}>
                <title>{`${h.handoff}: ${h.count}`}</title>
              </path>
              <text x={mx} y={(a.y + b.y) / 2 - 4} className="edge-label" textAnchor="middle">{h.count}</text>
            </g>
          )
        })}
        {Object.entries(pos).map(([name, p]) => {
          const s = stats[name]
          const hl = s ? HEALTH[s.health] : HEALTH.idle
          return (
            <g key={name} className="node" tabIndex={0} role="button" onClick={() => onPick(name)}
              onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && onPick(name)} aria-label={`${name}: ${s ? `${s.health}, ${s.runs} runs` : 'no runs'}`}>
              <rect x={p.x - 52} y={p.y - 19} width={104} height={38} rx={8} className={`node-box ${s ? '' : 'node-idle'}`} />
              <text x={p.x} y={p.y - 2} textAnchor="middle" className="node-name">{name}</text>
              <text x={p.x} y={p.y + 12} textAnchor="middle" className={`node-sub ${hl.cls}`}>{hl.icon} {s ? `${s.runs} runs` : 'no runs'}</text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

// --------------------------------------------------------------------------- drill-down
function DrillDown({ target, onClose }: { target: { correlation_id: string; run_id?: string }; onClose: () => void }) {
  const [wf, setWf] = useState<{ runs: AgentRun[]; traces: Trace[] } | null>(null)
  const [logs, setLogs] = useState<LogLine[] | null>(null)
  const [err, setErr] = useState('')
  const [sel, setSel] = useState<string | undefined>(target.run_id)
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    closeRef.current?.focus()
    setWf(null); setLogs(null); setErr('')
    adminApi.agentWorkflow(target.correlation_id).then(d => { setWf(d); setSel(s => s ?? d.runs[0]?.run_id) }).catch(e => setErr((e as Error).message))
    adminApi.agentLogs({ correlation_id: target.correlation_id, limit: 200 }).then(d => setLogs(d.lines.reverse())).catch(() => setLogs([]))
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [target.correlation_id]) // eslint-disable-line react-hooks/exhaustive-deps
  const run = wf?.runs.find(r => r.run_id === sel)
  const t0 = wf ? Math.min(...wf.runs.map(r => r.started)) : 0
  const total = wf ? Math.max(...wf.runs.map(r => (r.ended || r.started) - t0), 0.001) : 1
  return (
    <div className="drawer-back" onClick={onClose} role="presentation">
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Execution details" onClick={e => e.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <h2>Execution {wf && wf.runs[0] && <StatusPill s={wf.runs[0].status} />}</h2>
            <p className="dim small mono">correlation {target.correlation_id}{wf?.runs[0]?.trace_id ? ` · trace ${wf.runs[0].trace_id}` : ''}</p>
          </div>
          <button ref={closeRef} type="button" className="mini" onClick={onClose}>Close</button>
        </header>
        {err && <p className="msg-error">{err}</p>}
        {!wf && !err && <div className="obs-loading" aria-busy="true">Loading execution…</div>}
        {wf && (
          <>
            <h3>Agent timeline</h3>
            <ol className="tl">
              {wf.runs.map(r => (
                <li key={r.run_id}>
                  <button type="button" className={`tl-row ${r.run_id === sel ? 'on' : ''}`} onClick={() => setSel(r.run_id)} aria-pressed={r.run_id === sel}>
                    <span className="tl-name">{r.handoff_from && <span className="dim small">{r.handoff_from} → </span>}<b>{r.agent}</b></span>
                    <span className="tl-track"><span className={`tl-bar st-${r.status}`}
                      style={{ left: `${((r.started - t0) / total) * 100}%`, width: `${Math.max(((r.ended - r.started) / total) * 100, 0.8)}%` }} /></span>
                    <span className="mono small">{fmtMs(r.latency_ms)}</span>
                    <StatusPill s={r.status} />
                  </button>
                </li>
              ))}
            </ol>
            {run && (
              <div className="run-detail">
                <h3>{run.agent} <span className="dim small mono">run {run.run_id}</span></h3>
                <dl>
                  <dt>Status</dt><dd><StatusPill s={run.status} /> · {fmtMs(run.latency_ms)} · started {clock(run.started)}{run.parent_run_id && <span className="dim"> · parent {run.parent_run_id}</span>}</dd>
                  <dt>Handoff</dt><dd>{run.handoff_from || '—'}</dd>
                  <dt>Input</dt><dd>{run.input_summary || '—'}</dd>
                  <dt>Output</dt><dd>{run.output_summary || '—'}</dd>
                  <dt>LLM</dt><dd>{run.llm_calls} call{run.llm_calls === 1 ? '' : 's'}{run.models.length ? ` · ${run.models.join(', ')}` : ''} · tokens {fmtNum(run.tokens_in)} in / {fmtNum(run.tokens_out)} out · {fmtCost(run.cost_usd)}{run.retries ? ` · ${run.retries} retries` : ''}</dd>
                  <dt>Tools</dt>
                  <dd>{run.tool_calls.length === 0 ? '—' : (
                    <table className="mini-table">
                      <thead><tr><th>Tool</th><th>Arguments</th><th>Result</th><th>Time</th></tr></thead>
                      <tbody>{run.tool_calls.map((t, i) => (
                        <tr key={i}><td className="mono">{t.tool}</td><td className="dim">{t.args}</td><td>{t.ok ? <StatusPill s="ok" /> : <><StatusPill s="error" /> <span className="small">{t.error}</span></>}</td><td>{t.ms} ms</td></tr>))}
                      </tbody>
                    </table>)}</dd>
                  {run.error && <><dt>Error</dt><dd className="err-box">{run.error}</dd></>}
                </dl>
              </div>
            )}
            <h3>Trace</h3>
            {wf.traces.length ? wf.traces.map(t => <TraceView key={t.trace_id} t={t} />)
              : <p className="dim small">This trace has left the recent-trace buffer (latest 50 traces are kept).</p>}
            <h3>Logs for this execution</h3>
            {logs == null ? <div className="obs-loading">Loading logs…</div> : logs.length === 0 ? <p className="dim small">No log lines found for this correlation id.</p> : (
              <div className="log-list compact">{logs.map((l, i) => <LogRow key={i} l={l} />)}</div>
            )}
          </>
        )}
      </aside>
    </div>
  )
}

function LogRow({ l, onOpen }: { l: LogLine; onOpen?: (cid: string, rid?: string) => void }) {
  const cid = (l.correlation_id as string) || (l.request_id && l.request_id !== '-' ? l.request_id : '')
  const extras = ['agent', 'status', 'latency_ms', 'path', 'model', 'error'].filter(k => l[k] !== undefined && l[k] !== '' && l[k] !== null)
  return (
    <div className={`log-row lv-${l.level}`}>
      <span className="mono dim log-ts">{l.ts?.slice(11, 23)}</span>
      <span className={`log-level lv-${l.level}`}>{l.level}</span>
      <span className="mono log-logger">{l.logger}</span>
      <span className="log-msg">{l.msg}{extras.map(k => <span key={k} className="log-kv"> {k}=<b>{String(l[k])}</b></span>)}</span>
      {cid && onOpen ? <button type="button" className="link mono small" onClick={() => onOpen(cid, l.run_id as string | undefined)}>{cid}</button> : <span className="mono dim small">{cid}</span>}
    </div>
  )
}

// --------------------------------------------------------------------------- page
export function AgentsDashboard() {
  const [unlocked, setUnlocked] = useState(() => !!adminToken.get())
  const [range, setRange] = useState<Range>('1h')
  const [agent, setAgent] = useState('')
  const [refresh, setRefresh] = useState(5)
  const [summary, setSummary] = useState<AgentSummary | null>(null)
  const [points, setPoints] = useState<TsPoint[]>([])
  const [tools, setTools] = useState<ToolStat[]>([])
  const [errors, setErrors] = useState<ErrorGroup[]>([])
  const [files, setFiles] = useState<CsvFile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updated, setUpdated] = useState(0)
  const [drill, setDrill] = useState<{ correlation_id: string; run_id?: string } | null>(null)
  // runs explorer
  const [runs, setRuns] = useState<AgentRun[] | null>(null)
  const [status, setStatus] = useState('')
  const [q, setQ] = useState('')
  // logs
  const [logs, setLogs] = useState<LogLine[] | null>(null)
  const [logLevel, setLogLevel] = useState('')
  const [logQ, setLogQ] = useState('')
  const [logAgentsOnly, setLogAgentsOnly] = useState(false)
  const [hidePolling, setHidePolling] = useState(true)

  const fail = (e: unknown) => {
    if (e instanceof AdminAuthError) { adminToken.clear(); setUnlocked(false) } else setError((e as Error).message || 'Request failed')
  }

  const loadOverview = useCallback(async () => {
    try {
      const [s, t, tl, er, f] = await Promise.all([adminApi.agentSummary(range, agent), adminApi.agentTimeseries(range, agent),
        adminApi.agentTools(range, agent), adminApi.agentErrors(range, agent), adminApi.agentCsvFiles()])
      setSummary(s); setPoints(t.points); setTools(tl.tools); setErrors(er.errors); setFiles(f.files)
      setError(''); setUpdated(Date.now())
    } catch (e) { fail(e) } finally { setLoading(false) }
  }, [range, agent])

  const loadRuns = useCallback(async () => {
    try { setRuns((await adminApi.agentRuns({ agent, status, q, range, limit: 200 })).runs) } catch (e) { fail(e) }
  }, [agent, status, q, range])

  const loadLogs = useCallback(async () => {
    try {
      setLogs((await adminApi.agentLogs({ q: logQ, level: logLevel, agent, logger: logAgentsOnly ? 'agent_runs' : '', limit: 150, hide_polling: hidePolling })).lines)
    } catch (e) { fail(e) }
  }, [logQ, logLevel, agent, logAgentsOnly, hidePolling])

  useEffect(() => { if (unlocked) { setLoading(true); loadOverview() } }, [unlocked, loadOverview])
  useEffect(() => { if (unlocked) { const t = window.setTimeout(loadRuns, 250); return () => window.clearTimeout(t) } }, [unlocked, loadRuns])
  useEffect(() => { if (unlocked) { const t = window.setTimeout(loadLogs, 250); return () => window.clearTimeout(t) } }, [unlocked, loadLogs])
  useEffect(() => {
    if (!unlocked || !refresh) return
    const t = window.setInterval(() => { if (!document.hidden) { loadOverview(); loadRuns(); loadLogs() } }, refresh * 1000)
    return () => window.clearInterval(t)
  }, [unlocked, refresh, loadOverview, loadRuns, loadLogs])

  const series = useMemo(() => ({
    runs: points.map(p => ({ t: p.t, ok: p.ok, error: p.error, other: p.other })),
    latency: points.map(p => ({ t: p.t, p50: p.latency_p50_ms, p95: p.latency_p95_ms })),
    tokens: points.map(p => ({ t: p.t, in: p.tokens_in, out: p.tokens_out })),
    tools: points.map(p => ({ t: p.t, ok: p.tool_calls - p.tool_errors, err: p.tool_errors })),
  }), [points])

  if (!unlocked) return <Unlock onDone={() => setUnlocked(true)} />

  const T = summary?.totals
  const now = summary?.now ?? Date.now() / 1000
  const priced = !!summary && (summary.prices_per_mtok.in > 0 || summary.prices_per_mtok.out > 0)
  const empty = !!summary && T!.runs === 0
  const open = (cid: string, rid?: string) => setDrill({ correlation_id: cid, run_id: rid })

  return (
    <main className="obs">
      <div className="obs-toolbar" role="toolbar" aria-label="Dashboard filters">
        <div className="seg" role="group" aria-label="Time range">
          {RANGES.map(r => <button key={r.id} type="button" className={range === r.id ? 'on' : ''} aria-pressed={range === r.id} onClick={() => setRange(r.id)}>{r.label}</button>)}
        </div>
        <label className="obs-field"><span>Agent</span>
          <select value={agent} onChange={e => setAgent(e.target.value)}>
            <option value="">All agents</option>
            {(summary?.agents ?? []).map(a => <option key={a.agent} value={a.agent}>{a.agent}</option>)}
            {agent && !summary?.agents.some(a => a.agent === agent) && <option value={agent}>{agent}</option>}
          </select>
        </label>
        <label className="obs-field"><span>Auto-refresh</span>
          <select value={refresh} onChange={e => setRefresh(Number(e.target.value))}>
            {REFRESH.map(r => <option key={r.s} value={r.s}>{r.label}</option>)}
          </select>
        </label>
        <button type="button" className="mini" onClick={() => { loadOverview(); loadRuns(); loadLogs() }}>Refresh</button>
        <span className="dim small obs-updated" aria-live="polite">
          {loading ? 'Loading…' : updated ? `Updated ${new Date(updated).toLocaleTimeString()}` : ''}
          {summary && ` · ${fmtNum(summary.runs_in_buffer)} runs in memory${summary.history_loaded ? ` (${fmtNum(summary.history_loaded)} reloaded from CSV)` : ''}`}
        </span>
      </div>

      {error && (
        <div className="obs-error" role="alert">
          <b>Couldn't load observability data:</b> {error}. {summary ? 'Showing the last data received.' : ''}
          <button type="button" className="mini" onClick={() => { setError(''); loadOverview() }}>Retry</button>
        </div>
      )}

      {loading && !summary ? (
        <div className="kpis" aria-busy="true">{Array.from({ length: 8 }, (_, i) => <div key={i} className="kpi skeleton" />)}</div>
      ) : summary && T && (
        <section className="kpis" aria-label="Key metrics">
          <Kpi label="Questions (turns)" value={fmtNum(T.turns)} sub={`${fmtNum(T.runs)} agent runs`} />
          <Kpi label="Success rate" value={T.success_rate == null ? '—' : `${T.success_rate}%`} sub={`${fmtNum(T.ok)} ok of ${fmtNum(T.runs)}`}
            tone={T.success_rate == null ? undefined : T.success_rate >= 99 ? 'good' : T.success_rate >= 90 ? 'warn' : 'bad'} />
          <Kpi label="Errors" value={fmtNum(T.errors)} sub={`${fmtNum(T.tool_errors)} tool errors`} tone={T.errors ? 'bad' : 'good'} />
          <Kpi label="Turn latency p95" value={fmtMs(T.turn_latency_p95_ms)} sub={`p50 ${fmtMs(T.turn_latency_p50_ms)}`} />
          <Kpi label="Tokens" value={fmtNum(T.tokens_in + T.tokens_out)} sub={`${fmtNum(T.tokens_in)} in · ${fmtNum(T.tokens_out)} out · ${fmtNum(T.llm_calls)} LLM calls`} />
          <Kpi label="LLM cost" value={fmtCost(T.cost_usd)}
            sub={priced ? `$${summary.prices_per_mtok.in} in / $${summary.prices_per_mtok.out} out per MTok` : 'Local Ollama: no per-token charge'} />
          <Kpi label="Tool calls" value={fmtNum(T.tool_calls)} sub={T.tool_calls ? `${((100 * T.tool_errors) / T.tool_calls).toFixed(1)}% failed` : 'none'} tone={T.tool_errors ? 'warn' : undefined} />
          <Kpi label="Retries · handoffs" value={`${fmtNum(T.retries)} · ${fmtNum(T.handoffs)}`} sub={T.active ? `${T.active} running now` : 'nothing running now'} tone={T.retries ? 'warn' : undefined} />
        </section>
      )}

      {empty && (
        <Empty>
          No agent runs {agent ? `for ${agent} ` : ''}in the last {RANGES.find(r => r.id === range)!.label}.
          {' '}Ask a question in the Match tab, or <button type="button" className="link" onClick={() => setRange('24h')}>widen to 24 hours</button>.
        </Empty>
      )}

      {summary && !empty && (
        <>
          <div className="viz-grid">
            <StackedBars title="Agent runs" hint={`Per ${Math.round((points[1]?.t ?? 0) - (points[0]?.t ?? 0)) || '-'} s, by outcome`} points={series.runs}
              series={[{ key: 'ok', label: '✓ Succeeded', color: GOOD }, { key: 'error', label: '✕ Failed', color: CRIT }, { key: 'other', label: '! Degraded / blocked / cancelled', color: WARN }]} />
            <LineChart title={agent ? `${agent} latency` : 'Turn latency'} hint={agent ? 'Per run of this agent' : 'Whole question, end to end'} points={series.latency}
              series={[{ key: 'p50', label: 'p50', color: S1 }, { key: 'p95', label: 'p95', color: S2 }]} fmt={v => fmtMs(v)} />
            <LineChart title="Token usage" hint="LLM tokens per bucket" points={series.tokens}
              series={[{ key: 'in', label: 'Input tokens', color: S1 }, { key: 'out', label: 'Output tokens', color: S2 }]} fmt={v => fmtNum(Math.round(v))} />
            <StackedBars title="Tool calls" hint="Per bucket, by result" points={series.tools}
              series={[{ key: 'ok', label: '✓ Succeeded', color: S1 }, { key: 'err', label: '✕ Failed', color: CRIT }]} />
          </div>

          <div className="obs-cols">
            <Panel title="Agent status" sub="Health from the last 5 minutes: failing ≥ 50% errors, degraded = any error, idle = no runs.">
              <div className="agent-cards">
                {summary.agents.map(a => {
                  const h = HEALTH[a.health]
                  return (
                    <button key={a.agent} type="button" className={`agent-card ${agent === a.agent ? 'on' : ''}`} onClick={() => setAgent(agent === a.agent ? '' : a.agent)}
                      aria-pressed={agent === a.agent} title="Filter the dashboard to this agent">
                      <div className="agent-card-top"><b>{a.agent}</b><span className={`pill ${h.cls}`}><span aria-hidden="true">{h.icon}</span> {a.health}</span></div>
                      <div className="agent-card-stats">
                        <span><b>{a.runs}</b> runs</span><span><b>{a.success_rate}%</b> ok</span><span>p95 <b>{fmtMs(a.latency_p95_ms)}</b></span>
                        <span><b>{fmtNum(a.tokens_in + a.tokens_out)}</b> tokens</span><span><b>{a.tool_calls}</b> tools</span>
                        {a.retries > 0 && <span><b>{a.retries}</b> retries</span>}{a.active > 0 && <span><b>{a.active}</b> running</span>}
                      </div>
                      <div className="dim small">last run {ago(a.last_run, now)} · <StatusPill s={a.last_status} /></div>
                    </button>
                  )
                })}
              </div>
            </Panel>
            <Panel title="Handoffs between agents" sub="Line width = how often work passed along that path. Click an agent to filter.">
              <HandoffGraph summary={summary} onPick={a => setAgent(a === agent ? '' : a)} />
            </Panel>
          </div>

          <div className="obs-cols">
            <Panel title="Errors" sub="Grouped by agent and message, newest first. Click to open the latest execution.">
              {errors.length === 0 ? <Empty>No errors in this range.</Empty> : (
                <div className="table-wrap"><table className="obs-table">
                  <thead><tr><th>Agent</th><th>Where</th><th>Message</th><th>Count</th><th>Last seen</th></tr></thead>
                  <tbody>{errors.map((g, i) => (
                    <tr key={i} tabIndex={0} onClick={() => open(g.last_correlation_id, g.last_run_id)} onKeyDown={e => e.key === 'Enter' && open(g.last_correlation_id, g.last_run_id)}>
                      <td><b>{g.agent}</b></td><td className="small">{g.kind}</td><td className="err-msg">{g.message}</td><td>{g.count}</td><td className="small">{ago(g.last, now)}</td>
                    </tr>))}
                  </tbody>
                </table></div>
              )}
            </Panel>
            <Panel title="Tools" sub="Every tool call made by the agents in this range.">
              {tools.length === 0 ? <Empty>No tool calls in this range.</Empty> : (
                <div className="table-wrap"><table className="obs-table">
                  <thead><tr><th>Tool</th><th>Calls</th><th>Failed</th><th>Avg</th><th>p95</th><th>Used by</th></tr></thead>
                  <tbody>{tools.map(t => (
                    <tr key={t.tool}><td className="mono">{t.tool}</td><td>{t.calls}</td>
                      <td className={t.errors ? 'bad' : ''}>{t.errors ? `${t.errors} (${t.error_rate}%)` : '0'}</td>
                      <td>{fmtMs(t.avg_ms)}</td><td>{fmtMs(t.p95_ms)}</td><td className="small">{t.agents.join(', ')}</td></tr>))}
                  </tbody>
                </table></div>
              )}
            </Panel>
          </div>
        </>
      )}

      <Panel title="Executions" sub="Every agent run. Search by run id, correlation id, trace id or question text; click a row for the full execution."
        actions={<>
          <label className="sr-only" htmlFor="obs-status">Status</label>
          <select id="obs-status" className="obs-input" value={status} onChange={e => setStatus(e.target.value)}>
            {STATUSES.map(s => <option key={s} value={s}>{s || 'Any status'}</option>)}
          </select>
          <label className="sr-only" htmlFor="obs-q">Search runs</label>
          <input id="obs-q" className="obs-input mono" value={q} onChange={e => setQ(e.target.value)} placeholder="Run / correlation / trace id or text" />
        </>}>
        {runs == null ? <div className="obs-loading">Loading runs…</div> : runs.length === 0 ? <Empty>No runs match these filters.</Empty> : (
          <div className="table-wrap"><table className="obs-table runs-table">
            <thead><tr><th>Time</th><th>Agent</th><th>Status</th><th>Latency</th><th>Tokens</th><th>Tools</th><th>From</th><th>Input</th><th>Run id</th></tr></thead>
            <tbody>{runs.map(r => (
              <tr key={r.run_id} tabIndex={0} onClick={() => open(r.correlation_id, r.run_id)} onKeyDown={e => e.key === 'Enter' && open(r.correlation_id, r.run_id)}>
                <td className="mono small">{clock(r.started)}</td><td><b>{r.agent}</b></td><td><StatusPill s={r.status} /></td>
                <td>{fmtMs(r.latency_ms)}</td><td>{r.tokens_in + r.tokens_out ? fmtNum(r.tokens_in + r.tokens_out) : '—'}</td>
                <td className={r.tool_errors ? 'bad' : ''}>{r.tool_call_count || '—'}</td><td className="dim small">{r.handoff_from || '—'}</td>
                <td className="clip">{r.input_summary}</td><td className="mono small">{r.run_id}</td>
              </tr>))}
            </tbody>
          </table></div>
        )}
      </Panel>

      <Panel title="Logs" sub={<>Newest first, from <span className="mono">backend/var/app.log</span> (already redacted). Click a correlation id to open that execution.</>}
        actions={<>
          <label className="sr-only" htmlFor="log-level">Minimum level</label>
          <select id="log-level" className="obs-input" value={logLevel} onChange={e => setLogLevel(e.target.value)}>
            <option value="">All levels</option><option value="INFO">Info +</option><option value="WARNING">Warning +</option><option value="ERROR">Error +</option>
          </select>
          <label className="row small"><input type="checkbox" checked={logAgentsOnly} onChange={e => setLogAgentsOnly(e.target.checked)} /> Agent runs only</label>
          <label className="row small" title="Hide the request lines of this dashboard and the live ticker polling the API"><input type="checkbox" checked={hidePolling} onChange={e => setHidePolling(e.target.checked)} /> Hide polling</label>
          <label className="sr-only" htmlFor="log-q">Search logs</label>
          <input id="log-q" className="obs-input" value={logQ} onChange={e => setLogQ(e.target.value)} placeholder="Search log text" />
        </>}>
        {logs == null ? <div className="obs-loading">Loading logs…</div> : logs.length === 0 ? <Empty>No log lines match.</Empty> : (
          <div className="log-list" role="log" aria-live="off">{logs.map((l, i) => <LogRow key={i} l={l} onOpen={open} />)}</div>
        )}
      </Panel>

      <Panel title="CSV exports" sub="backend/var/metrics/: one row per agent run, one per tool call, and a per-agent metrics snapshot every minute.">
        {files.length === 0 ? <Empty>No CSV files yet.</Empty> : (
          <ul className="csv-list">{files.map(f => (
            <li key={f.name}><button type="button" className="link mono" onClick={() => downloadCsv(f.name).catch(e => setError((e as Error).message))}>{f.name}</button>
              <span className="dim small"> {(f.bytes / 1024).toFixed(1)} KB · {new Date(f.modified * 1000).toLocaleTimeString()}</span></li>))}
          </ul>
        )}
      </Panel>

      {drill && <DrillDown target={drill} onClose={() => setDrill(null)} />}
    </main>
  )
}
