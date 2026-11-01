import { useEffect, useState } from 'react'
import { AdminAuthError, adminApi, adminToken, opsStatus } from '../api'
import type { OpsStatus, Trace } from '../types'

const LEVEL_TEXT = ['Normal', 'Busy: thinking off, shorter answers', 'LLM offline: data-only answers']
const COMPONENT_TEXT: Record<string, string> = {
  ollama_chat: 'Chat model', ollama_embed: 'Embeddings', chroma: 'Vector DB (Chroma)', stats_db: 'Stats DB (SQLite)', feed: 'Live feed',
}

function fmtUptime(s: number) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`
}

export function TraceView({ t }: { t: Trace }) {
  const total = Math.max(t.duration_ms, 1)
  return (
    <details className="trace">
      <summary>
        <span className="mono">{t.root}</span>
        <span className="mono dim">{String(t.spans[0]?.attributes['chat.path'] ?? t.spans.find(s => s.attributes['chat.path'])?.attributes['chat.path'] ?? '')}</span>
        <span className="mono">{t.duration_ms.toFixed(0)} ms</span>
      </summary>
      <div className="spans">
        {t.spans.map(s => (
          <div key={s.span_id} className="span-row" title={JSON.stringify(s.attributes, null, 1)}>
            <span className="span-name mono">{s.name}</span>
            <span className="span-track">
              <span className={`span-bar ${s.status === 'ERROR' ? 'err' : ''}`}
                style={{ left: `${(s.offset_ms / total) * 100}%`, width: `${Math.max((s.duration_ms / total) * 100, 0.8)}%` }} />
            </span>
            <span className="mono dim span-ms">{s.duration_ms.toFixed(0)}</span>
          </div>
        ))}
      </div>
    </details>
  )
}

export function OpsPanel() {
  const [status, setStatus] = useState<OpsStatus | null>(null)
  const [down, setDown] = useState(false)
  const [token, setToken] = useState(adminToken.get())
  const [tokenInput, setTokenInput] = useState('')
  const [traces, setTraces] = useState<Trace[]>([])
  const [errors, setErrors] = useState<{ ts: number; component: string; message: string }[]>([])
  const [adminErr, setAdminErr] = useState('')

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try { const s = await opsStatus(); if (alive) { setStatus(s); setDown(false) } }
      catch { if (alive) setDown(true) }
    }
    tick()
    const t = window.setInterval(tick, 3000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  useEffect(() => {
    if (!token) return
    let alive = true
    const tick = async () => {
      try {
        const [tr, er] = await Promise.all([adminApi.traces(), adminApi.errors()])
        if (alive) { setTraces(tr.traces); setErrors(er.errors); setAdminErr('') }
      } catch (e) {
        if (alive) handleAdminError(e)
      }
    }
    tick()
    const t = window.setInterval(tick, 4000)
    return () => { alive = false; window.clearInterval(t) }
  }, [token])

  const [checking, setChecking] = useState(false)

  // A stored token the server rejects locks the panel again with a clear message, instead of leaving dead buttons.
  const handleAdminError = (e: unknown) => {
    if (e instanceof AdminAuthError) {
      adminToken.clear()
      setToken('')
      setAdminErr('The saved admin token was rejected, so the panel locked again. Paste the token from backend/.env.')
    } else {
      setAdminErr((e as Error).message)
    }
  }

  const run = async (fn: () => Promise<unknown>) => {
    try { await fn(); setAdminErr(''); setStatus(await opsStatus()) } catch (e) { handleAdminError(e) }
  }

  const unlock = async () => {
    const t = adminToken.normalize(tokenInput)
    if (!t) return
    setChecking(true)
    try {
      if (await adminToken.verify(t)) {
        adminToken.set(t); setToken(t); setTokenInput(''); setAdminErr('')
      } else {
        setAdminErr(/SESSION_SECRET/i.test(tokenInput)
          ? "That's the session secret (first line of backend/.env). Use the CRICKET_ADMIN_TOKEN line instead."
          : "That token doesn't match. Use the CRICKET_ADMIN_TOKEN line of backend/.env, not CRICKET_SESSION_SECRET.")
      }
    } catch {
      setAdminErr("Couldn't reach the API to check the token.")
    } finally {
      setChecking(false)
    }
  }

  if (down && !status) return <aside className="ops"><p className="msg-error">The API is unreachable. Is the backend running on port 8000?</p></aside>
  if (!status) return <aside className="ops"><p className="dim">Loading system status…</p></aside>

  const a = status.answers
  return (
    <aside className="ops" aria-label="System status">
      {down && <p className="msg-error">Lost contact with the API. Showing the last known state.</p>}
      <div className={`level level-${status.degrade_level}`}>
        <span className="label">Service level</span>
        <b>L{status.degrade_level} · {LEVEL_TEXT[status.degrade_level]}</b>
        {status.forced && <span className="dim">forced by operator</span>}
      </div>

      <h3>Components</h3>
      <ul className="components">
        {status.components.map(c => (
          <li key={c.name}>
            <span className={`dot ${c.up ? 'up' : 'down'}`} aria-hidden="true" />
            <span>{COMPONENT_TEXT[c.name] ?? c.name}</span>
            <span className="mono dim">{c.chaos ? 'failure injected' : c.breaker !== 'n/a' && c.breaker !== 'closed' ? `breaker ${c.breaker}` : c.note}</span>
            {token && (
              <button type="button" className={`mini ${c.chaos ? 'on' : ''}`} onClick={() => run(() => adminApi.chaos(c.name, !c.chaos))}>
                {c.chaos ? 'Restore' : 'Break'}
              </button>
            )}
          </li>
        ))}
      </ul>

      <h3>Traffic, last 10 min</h3>
      <dl className="kv">
        <dt>Answers</dt><dd>{a.answers}</dd>
        <dt>By path</dt><dd>{Object.entries(a.by_path).map(([k, v]) => `${k} ${v}`).join(' · ') || '—'}</dd>
        <dt>Latency p50 / p95</dt><dd>{a.latency_p50_ms ?? '—'} / {a.latency_p95_ms ?? '—'} ms</dd>
        <dt>LLM first word p50</dt><dd>{a.llm_first_token_p50_ms ?? '—'} ms</dd>
        <dt>LLM slots</dt><dd>{status.admission.inflight}/{status.admission.max_concurrency} busy · {status.admission.waiting}/{status.admission.max_queue} queued</dd>
        <dt>Answer cache</dt><dd>{(status.cache.hit_rate * 100).toFixed(0)}% hits · {status.cache.size} entries</dd>
        <dt>Rate limit</dt><dd>{status.limits.per_client_per_min}/min per fan, burst {status.limits.burst}</dd>
        <dt>Fans · tickers</dt><dd>{status.sessions} · {status.ticker_clients}</dd>
        <dt>Feed</dt><dd>ball {status.feed.ball}/{status.feed.of} · every {status.feed.interval_s}s{status.feed.paused ? ' · paused' : ''}</dd>
        <dt>Knowledge</dt><dd>{status.knowledge_docs} docs</dd>
        <dt>Models</dt><dd className="mono">{status.models.chat} · {status.models.think} · {status.models.embed}</dd>
        <dt>Uptime</dt><dd>{fmtUptime(status.uptime_s)}</dd>
      </dl>
      <p className="dim small">Prometheus metrics at <code>/metrics</code>.</p>

      <h3>Operator</h3>
      {!token ? (
        <form className="admin-login" onSubmit={e => { e.preventDefault(); unlock() }}>
          <label htmlFor="admin-token" className="dim small">Admin token (from backend/.env) unlocks failure injection, feed control and traces.</label>
          <div className="row">
            <input id="admin-token" type="password" value={tokenInput} onChange={e => setTokenInput(e.target.value)} autoComplete="off" />
            <button type="submit" disabled={!tokenInput || checking}>{checking ? 'Checking…' : 'Unlock'}</button>
          </div>
          {adminErr && <p className="msg-error">{adminErr}</p>}
        </form>
      ) : (
        <>
          <div className="row wrap">
            {status.feed.paused
              ? <button type="button" className="mini" onClick={() => run(() => adminApi.feed('resume'))}>Resume feed</button>
              : <button type="button" className="mini" onClick={() => run(() => adminApi.feed('pause'))}>Pause feed</button>}
            <button type="button" className="mini" onClick={() => run(() => adminApi.feed('step', 6))}>+1 over</button>
            <button type="button" className="mini" onClick={() => run(() => adminApi.feed('speed', 1))}>Fast (1s)</button>
            <button type="button" className="mini" onClick={() => run(() => adminApi.feed('speed', 4))}>Normal (4s)</button>
            <button type="button" className="mini" onClick={() => run(() => adminApi.feed('restart'))}>Restart match</button>
            <button type="button" className="mini" onClick={() => run(() => adminApi.clearCache())}>Clear cache</button>
          </div>
          <div className="row wrap">
            <span className="dim small">Force level:</span>
            {[null, 0, 1, 2].map(l => (
              <button key={String(l)} type="button" className="mini" onClick={() => run(() => adminApi.degrade(l))}>{l === null ? 'Auto' : `L${l}`}</button>
            ))}
            <button type="button" className="ghost mini" onClick={() => { adminToken.clear(); setToken('') }}>Lock</button>
          </div>
          {adminErr && <p className="msg-error">{adminErr}</p>}

          <h3>Recent traces</h3>
          {traces.length === 0 ? <p className="dim small">Ask a question to see its trace.</p> : traces.map(t => <TraceView key={t.trace_id} t={t} />)}

          <h3>Recent errors</h3>
          {errors.length === 0 ? <p className="dim small">No errors recorded.</p> : (
            <ul className="errors">
              {errors.slice(0, 8).map((e, i) => (
                <li key={i}><span className="mono dim">{new Date(e.ts * 1000).toLocaleTimeString()}</span> <b>{e.component}</b> {e.message}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </aside>
  )
}
