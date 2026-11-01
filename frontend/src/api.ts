import type { MatchState, OpsStatus, Trace } from './types'

const TOKEN_KEY = 'crickbot.session'
const ADMIN_KEY = 'crickbot.admin'

function store(): Storage | null {
  try { return window.sessionStorage } catch { return null }
}

export class RateLimitError extends Error {
  retryAfter: number
  constructor(retryAfter: number, message: string) { super(message); this.retryAfter = retryAfter }
}

let tokenPromise: Promise<string> | null = null

async function newSession(): Promise<string> {
  const r = await fetch('/api/session', { method: 'POST' })
  if (r.status === 429) throw new RateLimitError(Number(r.headers.get('Retry-After') ?? 10), 'Too many new sessions, wait a moment.')
  if (!r.ok) throw new Error(`Could not start a session (${r.status})`)
  const { token } = await r.json()
  store()?.setItem(TOKEN_KEY, token)
  return token
}

export function getToken(forceNew = false): Promise<string> {
  const saved = store()?.getItem(TOKEN_KEY)
  if (!forceNew && saved) return Promise.resolve(saved)
  if (!tokenPromise) tokenPromise = newSession().finally(() => { tokenPromise = null })
  return tokenPromise
}

export type ChatEvent = { type: string; [k: string]: unknown }

/** POST /api/chat and yield server-sent events. Retries once with a fresh session if the token expired. */
export async function* chatStream(question: string, think: boolean, signal: AbortSignal): AsyncGenerator<ChatEvent> {
  let token = await getToken()
  let r = await send(token)
  if (r.status === 401) {
    token = await getToken(true)
    r = await send(token)
  }
  if (r.status === 429) {
    const body = await r.json().catch(() => ({}))
    throw new RateLimitError(Number(r.headers.get('Retry-After') ?? body.retry_after ?? 5), body.detail ?? 'Slow down a little.')
  }
  if (!r.ok || !r.body) throw new Error(`The server answered ${r.status}. Try again.`)

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const line = frame.split('\n').find(l => l.startsWith('data: '))
      if (line) yield JSON.parse(line.slice(6))
    }
  }

  function send(t: string) {
    return fetch('/api/chat', {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${t}` },
      body: JSON.stringify({ question, think }),
    })
  }
}

export async function clearHistory() {
  const token = await getToken()
  await fetch('/api/chat/history', { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } })
}

export async function sendFeedback(messageId: string, rating: 1 | -1) {
  const token = await getToken()
  await fetch('/api/feedback', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ message_id: messageId, rating }),
  })
}

/**
 * Live ticker: server-sent events, with automatic fallback to ETag polling when the stream is refused
 * (ticker full) or keeps dropping. Returns an unsubscribe function.
 */
export function subscribeLive(onState: (s: MatchState) => void, onLink: (mode: 'stream' | 'polling' | 'offline') => void): () => void {
  let es: EventSource | null = null
  let pollTimer: number | undefined
  let failures = 0
  let etag = ''
  let stopped = false

  const poll = async () => {
    try {
      const r = await fetch('/api/live', { headers: etag ? { 'If-None-Match': etag } : {} })
      if (r.status === 200) { etag = r.headers.get('ETag') ?? ''; onState(await r.json()) }
      onLink('polling')
    } catch { onLink('offline') }
    if (!stopped) pollTimer = window.setTimeout(poll, 2000)
  }

  const connect = () => {
    es = new EventSource('/api/live/stream')
    es.onmessage = ev => {
      failures = 0
      onLink('stream')
      const msg = JSON.parse(ev.data)
      if (msg.type === 'state') onState(msg.state)
    }
    es.onerror = () => {
      es?.close()
      failures += 1
      if (stopped) return
      if (failures >= 3) { onLink('polling'); poll() }   // give up on streaming, poll instead
      else window.setTimeout(connect, 1000 * 2 ** failures)
    }
  }
  connect()
  return () => { stopped = true; es?.close(); window.clearTimeout(pollTimer) }
}

/** The caller's own session transcript (.txt): every turn, route, agent and tool call. */
export async function myTranscript(): Promise<{ name: string | null; content: string }> {
  const token = await getToken()
  const r = await fetch('/api/transcript', { headers: { Authorization: `Bearer ${token}` } })
  if (!r.ok) throw new Error(`Couldn't load the transcript (${r.status})`)
  return r.json()
}

export interface TranscriptFile { name: string; bytes: number; modified: number; turns: number }

export interface AgentToolCall { tool: string; args: string; ok: boolean; ms: number; error: string; ts: number }
export interface AgentRun {
  run_id: string; parent_run_id: string; correlation_id: string; trace_id: string; session: string; agent: string
  status: string; started: number; ended: number; started_utc: string; latency_ms: number; llm_calls: number; models: string[]
  tokens_in: number; tokens_out: number; cost_usd: number; retries: number; tool_calls: AgentToolCall[]; tool_call_count: number
  tool_errors: number; handoff_from: string; input_summary: string; output_summary: string; error: string
}
export type Health = 'healthy' | 'degraded' | 'failing' | 'idle'
export interface AgentStats {
  agent: string; runs: number; ok: number; errors: number; other_status: number; success_rate: number; latency_p50_ms: number
  latency_p95_ms: number | null; latency_max_ms: number; tokens_in: number; tokens_out: number; cost_usd: number; llm_calls: number
  tool_calls: number; tool_errors: number; retries: number; last_run: number; last_status: string; runs_last_5m: number
  errors_last_5m: number; health: Health; active: number
}
export interface AgentTotals {
  runs: number; turns: number; ok: number; errors: number; success_rate: number | null; turn_latency_p50_ms: number | null
  turn_latency_p95_ms: number | null; tokens_in: number; tokens_out: number; cost_usd: number; llm_calls: number; tool_calls: number
  tool_errors: number; retries: number; handoffs: number; active: number
}
export interface AgentSummary {
  since: number; app_started: number; now: number; runs_in_buffer: number; history_loaded: number
  prices_per_mtok: { in: number; out: number }; totals: AgentTotals; agents: AgentStats[]
  handoffs: { from: string; to: string; handoff: string; count: number }[]
}
export interface TsPoint {
  t: number; ok: number; error: number; other: number; tokens_in: number; tokens_out: number; cost_usd: number; tool_calls: number
  tool_errors: number; retries: number; llm_calls: number; latency_p50_ms: number | null; latency_p95_ms: number | null
}
export interface ToolStat { tool: string; calls: number; errors: number; error_rate: number; avg_ms: number; p95_ms: number | null; agents: string[] }
export interface ErrorGroup { agent: string; kind: string; message: string; count: number; first: number; last: number; last_run_id: string; last_correlation_id: string }
export interface LogLine { ts: string; level: string; logger: string; msg: string; request_id?: string; [k: string]: unknown }
export type Range = '15m' | '1h' | '6h' | '24h'
export interface CsvFile { name: string; bytes: number; modified: number }

/** Admin CSV download: the token goes in a header, so fetch the file and hand the browser a blob. */
export async function downloadCsv(name: string) {
  const r = await fetch(`/api/admin/agents/csv/${encodeURIComponent(name)}`, { headers: { 'X-Admin-Token': adminToken.get() } })
  if (r.status === 403) throw new AdminAuthError('Admin token rejected')
  if (!r.ok) throw new Error(`Download failed (${r.status})`)
  const url = URL.createObjectURL(await r.blob())
  const a = document.createElement('a')
  a.href = url; a.download = name; a.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 5000)
}

export async function suggestions(): Promise<string[]> {
  const r = await fetch('/api/suggestions')
  return r.ok ? (await r.json()).suggestions : []
}

export async function opsStatus(): Promise<OpsStatus> {
  const r = await fetch('/api/ops/status')
  if (!r.ok) throw new Error(String(r.status))
  return r.json()
}

// --- admin (token kept in sessionStorage only) -----------------------------------
export class AdminAuthError extends Error {}

export const adminToken = {
  get: () => store()?.getItem(ADMIN_KEY) ?? '',
  set: (t: string) => store()?.setItem(ADMIN_KEY, t),
  clear: () => store()?.removeItem(ADMIN_KEY),
  /** Forgiving paste: trims spaces, quotes and a copied "CRICKET_ADMIN_TOKEN=" prefix. */
  normalize: (raw: string) => raw.trim().replace(/^CRICKET_ADMIN_TOKEN\s*=\s*/i, '').replace(/^["']|["']$/g, '').trim(),
  /** True if the server accepts this token. */
  async verify(t: string): Promise<boolean> {
    const r = await fetch('/api/admin/limits', { headers: { 'X-Admin-Token': t } })
    return r.ok
  },
}

/** Query string from an object, dropping empty values. */
function qs(p: Record<string, unknown>): string {
  return new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)])).toString()
}

async function admin<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api/admin/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Admin-Token': adminToken.get() },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 403) throw new AdminAuthError('Admin token rejected')
  if (!r.ok) throw new Error(`Admin call failed (${r.status})`)
  return r.json()
}

export interface MetricPoint {
  ts: number; answers_per_min: number; llm_per_min: number; rate_limited_per_min: number; throttled_per_min: number
  shed_per_min: number; errors_per_min: number; blocked_per_min: number; tokens_out_per_min: number
  llm_inflight: number; llm_queue: number; latency_p50_ms: number | null; latency_p95_ms: number | null
  cache_hit_pct: number | null; degrade_level: number; feed_lag_s: number | null; components_down: number; ticker_clients: number
}
export interface AlertItem { key: string; severity: 'critical' | 'warning' | 'info'; title: string; detail: string; since: number }
export interface AlertEvent { ts: number; event: 'fired' | 'resolved'; key: string; severity: string; title: string; detail: string }
export interface Alerts { overall: 'healthy' | 'degraded' | 'down'; active: AlertItem[]; history: AlertEvent[] }
export interface Limits { rate_per_client_per_min: number; rate_burst_per_client: number; rate_global_per_s: number; llm_queue_max: number; llm_queue_timeout_s: number }

export async function opsTimeseries(minutes = 10): Promise<{ interval_s: number; points: MetricPoint[] }> {
  const r = await fetch(`/api/ops/timeseries?minutes=${minutes}`)
  if (!r.ok) throw new Error(String(r.status))
  return r.json()
}

export async function opsAlerts(): Promise<Alerts> {
  const r = await fetch('/api/ops/alerts')
  if (!r.ok) throw new Error(String(r.status))
  return r.json()
}

/** Runs a built-in traffic test on the server and streams its events. */
export async function* runSelfTest(name: string): AsyncGenerator<ChatEvent> {
  const r = await fetch(`/api/admin/selftest/${name}`, { method: 'POST', headers: { 'X-Admin-Token': adminToken.get() } })
  if (r.status === 403) throw new AdminAuthError('Admin token rejected')
  if (r.status === 409) throw new Error('Another test is already running')
  if (!r.ok || !r.body) throw new Error(`Test failed to start (${r.status})`)
  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, idx).split('\n').find(l => l.startsWith('data: '))
      buf = buf.slice(idx + 2)
      if (line) yield JSON.parse(line.slice(6))
    }
  }
}

export const adminApi = {
  traces: () => admin<{ traces: Trace[] }>('traces?limit=12'),
  errors: () => admin<{ errors: { ts: number; component: string; message: string }[] }>('errors'),
  chaos: (target: string, enabled: boolean) => admin('chaos', { target, enabled }),
  degrade: (level: number | null) => admin('degrade', { level }),
  feed: (action: string, value?: number) => admin('feed', { action, value }),
  clearCache: () => admin('cache/clear', {}),
  limits: () => admin<{ current: Limits; defaults: Limits }>('limits'),
  setLimits: (l: Partial<Limits>) => admin<{ current: Limits }>('limits', l),
  resetLimits: () => admin<{ current: Limits }>('limits/reset', {}),
  transcripts: () => admin<{ folder: string; files: TranscriptFile[] }>('transcripts'),
  agentSummary: (range: Range, agent = '') => admin<AgentSummary>(`agents/summary?${qs({ range, agent })}`),
  agentTimeseries: (range: Range, agent = '') => admin<{ bucket_s: number; points: TsPoint[] }>(`agents/timeseries?${qs({ range, agent })}`),
  agentTools: (range: Range, agent = '') => admin<{ tools: ToolStat[] }>(`agents/tools?${qs({ range, agent })}`),
  agentErrors: (range: Range, agent = '') => admin<{ errors: ErrorGroup[] }>(`agents/errors?${qs({ range, agent })}`),
  agentRuns: (p: { agent?: string; status?: string; q?: string; limit?: number; range?: Range }) => admin<{ runs: AgentRun[] }>(`agents/runs?${qs(p)}`),
  agentRun: (runId: string) => admin<AgentRun>(`agents/runs/${encodeURIComponent(runId)}`),
  agentWorkflow: (cid: string) => admin<{ correlation_id: string; runs: AgentRun[]; traces: Trace[] }>(`agents/workflow/${encodeURIComponent(cid)}`),
  agentLogs: (p: { q?: string; level?: string; logger?: string; agent?: string; correlation_id?: string; limit?: number; hide_polling?: boolean }) =>
    admin<{ lines: LogLine[]; scanned: number; file: string }>(`agents/logs?${qs(p)}`),
  agentCsvFiles: () => admin<{ folder: string; files: CsvFile[] }>('agents/csv'),
  transcript: (name: string) => admin<{ name: string; content: string }>(`transcripts/${encodeURIComponent(name)}`),
}
