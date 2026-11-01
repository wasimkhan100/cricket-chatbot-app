export interface Batter { name: string; runs: number; balls: number; fours: number; sixes: number; out: string; strike_rate: number }
export interface Bowler { name: string; balls: number; runs: number; wickets: number; overs: string; economy: number }
export interface Innings {
  number: number; batting_team: string; bowling_team: string; runs: number; wickets: number; overs: string
  run_rate: number; extras: number; fours: number; sixes: number; fall_of_wickets: string[]
  batters: Batter[]; bowlers: Bowler[]
}
export interface Commentary { over: string; text: string; kind: 'ball' | 'boundary' | 'wicket' | 'break' | 'ai'; version: number }
export interface MatchState {
  match_id: number; title: string; season: number; venue: string; team1: string; team2: string; toss: string
  version: number; status: 'scheduled' | 'live' | 'innings_break' | 'complete'; result: string; last_event: string
  last_balls: string[]; striker: string; non_striker: string; bowler: string; updated_at: number; as_of: string
  chase: { target: number; runs_needed: number; balls_left: number; required_run_rate: number } | null
  commentary: Commentary[]; innings: Innings[]
}

export type AnswerPath = 'template' | 'cache' | 'llm' | 'coalesced' | 'degraded' | 'shed' | 'blocked' | 'fact' | 'knowledge' | 'scope' | 'off_topic'

export interface ToolEvent { name: string; args: Record<string, unknown>; ok: boolean; ms: number | null; planned?: boolean; hits?: string[]; error?: string; agent?: string; chosen_by?: 'planner' | 'llm' }
export interface RouteEvent { routes: string[]; method: 'rules' | 'llm' | 'default'; reason: string }
export interface AgentEvent { name: string; status: string; ms: number | null; output?: string }
export interface Source { type: 'live' | 'doc' | 'stats'; id: string }

export interface DoneEvent {
  answer: string; path: AnswerPath; as_of: string; sources: Source[]; verified: boolean; unverified: number[]
  latency_ms: number; first_token_ms: number | null; tokens: number; thinking_chars: number; model: string | null
  reason: string | null; retry_after: number | null; degrade_level: number; removed_sentences?: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'bot'
  text: string
  thinking?: string
  tools?: ToolEvent[]
  route?: RouteEvent
  agents?: AgentEvent[]
  done?: DoneEvent
  pending?: boolean
  error?: string
  model?: string | null
  feedback?: -1 | 1
}

export interface Component { name: string; up: boolean; breaker: string; chaos: boolean; note: string; last_error?: string | null }
export interface OpsStatus {
  degrade_level: number; degrade_name: string; forced: boolean; components: Component[]
  admission: { inflight: number; waiting: number; max_concurrency: number; max_queue: number }
  feed: { ball: number; of: number; paused: boolean; interval_s: number; version: number; status: string }
  cache: { size: number; hits: number; misses: number; hit_rate: number }
  sessions: number; ticker_clients: number; knowledge_docs: number; uptime_s: number
  models: { chat: string; think: string; embed: string; available: string[] }
  answers: { answers: number; by_path: Record<string, number>; latency_p50_ms: number | null; latency_p95_ms: number | null; llm_first_token_p50_ms: number | null; tokens_out: number }
  limits: { per_client_per_min: number; burst: number; global_per_s: number }
}

export interface TraceSpan { name: string; span_id: string; parent_id: string | null; duration_ms: number; offset_ms: number; status: string; attributes: Record<string, unknown> }
export interface Trace { trace_id: string; root: string; duration_ms: number; spans: TraceSpan[] }
