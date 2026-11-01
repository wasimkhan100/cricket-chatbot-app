import { useCallback, useEffect, useRef, useState } from 'react'
import { chatStream, clearHistory, myTranscript, RateLimitError, sendFeedback, suggestions as fetchSuggestions } from '../api'
import type { AgentEvent, ChatMessage, DoneEvent, MatchState, RouteEvent, ToolEvent } from '../types'

const PATH_LABEL: Record<string, string> = {
  template: 'Live data', cache: 'Cached', llm: 'AI answer', coalesced: 'Shared answer',
  degraded: 'Fallback', shed: 'Busy fallback', blocked: 'Declined',
  fact: 'From stats DB', knowledge: 'Rule book', scope: 'Not in my data', off_topic: 'Off topic',
}
const TOOL_LABEL: Record<string, string> = {
  retrieve_notes: 'Searched record book', get_player_stats: 'Player stats', head_to_head: 'Head to head',
  season_summary: 'Season summary', team_season_record: 'Team season', top_players: 'Leaderboard',
  get_live_match: 'Live scorecard', search_cricket_knowledge: 'Knowledge search',
  get_live_score: 'Live score', get_ball_by_ball: 'Ball by ball', win_probability: 'Win chance', get_weather: 'Weather (mock)',
  get_pitch_report: 'Pitch report (mock)', query_player_stats: 'Player stats', phase_stats: 'Phase stats',
  batter_vs_bowler: 'Batter v bowler', dismissal_breakdown: 'Dismissals', venue_stats: 'Venue record', toss_impact: 'Toss impact',
  find_matches: 'Match list', player_of_match_awards: 'Player of the match', search_rules: 'Rule book',
  team_titles: 'Titles', milestone: 'Milestone',
}

let seq = 0
const uid = () => `${Date.now().toString(36)}-${(seq++).toString(36)}`

function Meta({ done }: { done: DoneEvent }) {
  return (
    <div className="msg-meta">
      <span className={`path path-${done.path}`}>{PATH_LABEL[done.path] ?? done.path}</span>
      <span className="mono">{(done.latency_ms / 1000).toFixed(1)}s</span>
      {done.path === 'llm' && done.first_token_ms != null && <span className="mono">first word {(done.first_token_ms / 1000).toFixed(1)}s</span>}
      {done.model && <span className="mono">{done.model}</span>}
      <span className="mono dim">{done.as_of}</span>
      {done.path === 'llm' && (done.verified
        ? <span className="verified" title="Every number matches the match state or a tool result">✓ figures checked</span>
        : <span className="unverified" title={`Not found in data: ${done.unverified.join(', ')}`}>⚠ unverified figures</span>)}
      {!!done.removed_sentences && (
        <span className="unverified" title="The model wrote a figure that isn't in the data, so that sentence was removed">
          {done.removed_sentences} unsupported sentence{done.removed_sentences > 1 ? 's' : ''} removed
        </span>)}
    </div>
  )
}

function Agents({ route, agents }: { route?: RouteEvent; agents?: AgentEvent[] }) {
  if (!route) return null
  return (
    <div className="agents" aria-label="Agents that answered">
      <span className="agent agent-router" title={route.reason}>Router ({route.method}) → {route.routes.join(', ')}</span>
      {(agents ?? []).map((a, i) => (
        <span key={i} className="agent" title={a.output ?? a.status}>
          {a.name} <span className="dim">{a.status}{a.ms != null ? ` · ${a.ms < 1000 ? `${a.ms}ms` : `${(a.ms / 1000).toFixed(1)}s`}` : ''}</span>
        </span>
      ))}
    </div>
  )
}

function TranscriptViewer({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<{ name: string | null; content: string } | null>(null)
  const [err, setErr] = useState('')
  const load = () => myTranscript().then(setData).catch(e => setErr((e as Error).message))
  useEffect(() => { load() }, [])
  return (
    <div className="modal-back" role="presentation" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Session transcript" onClick={e => e.stopPropagation()}>
        <header>
          <div>
            <h3>Session transcript</h3>
            <p className="dim small">{data?.name ? `backend/var/conversations/${data.name}` : 'Every question, route, agent, tool call and answer in this session.'}</p>
          </div>
          <div className="row">
            <button type="button" className="ghost" onClick={load}>Refresh</button>
            <button type="button" className="ghost" onClick={onClose} aria-label="Close">Close</button>
          </div>
        </header>
        {err ? <p className="msg-error">{err}</p> : <pre className="transcript">{data?.content ?? 'Loading…'}</pre>}
      </div>
    </div>
  )
}

function Tools({ tools }: { tools: ToolEvent[] }) {
  return (
    <div className="tools">
      {tools.map((t, i) => (
        <span key={i} className={`tool ${t.ok ? '' : 'tool-fail'}`} title={`${t.agent ? `${t.agent} · ` : ''}${t.chosen_by === 'llm' ? 'chosen by the agent · ' : ''}${JSON.stringify(t.args)}`}>
          {t.ok ? '↳' : '✕'} {TOOL_LABEL[t.name] ?? t.name}
          {typeof t.args?.player === 'string' && `: ${t.args.player}`}
          {typeof t.args?.season === 'number' && ` ${t.args.season}`}
          {t.ms != null && <span className="dim"> {t.ms}ms</span>}
        </span>
      ))}
    </div>
  )
}

function Message({ m, onFeedback }: { m: ChatMessage; onFeedback: (m: ChatMessage, r: 1 | -1) => void }) {
  const [showThinking, setShowThinking] = useState(false)
  if (m.role === 'user') return <div className="msg msg-user">{m.text}</div>
  return (
    <div className={`msg msg-bot ${m.pending ? 'pending' : ''}`}>
      <Agents route={m.route} agents={m.agents} />
      {m.tools && m.tools.length > 0 && <Tools tools={m.tools} />}
      {m.thinking && (
        <div className="thinking">
          <button type="button" className="link" onClick={() => setShowThinking(v => !v)} aria-expanded={showThinking}>
            {m.pending && !m.text ? 'Thinking…' : `Thought for ${m.thinking.length.toLocaleString()} chars`} {showThinking ? '▴' : '▾'}
          </button>
          {showThinking && <pre>{m.thinking}</pre>}
        </div>
      )}
      <div className="msg-text">
        {m.text || (m.pending ? <span className="typing" aria-label="Answering"><i /><i /><i /></span> : null)}
      </div>
      {m.error && <div className="msg-error">{m.error}</div>}
      {m.done && (
        <>
          <Meta done={m.done} />
          {m.done.sources.filter(s => s.type !== 'live').length > 0 && (
            <div className="sources">
              Sources: {m.done.sources.filter(s => s.type !== 'live').map(s => s.id).join(' · ')}
            </div>
          )}
          {m.done.path === 'llm' && (
            <div className="feedback">
              <button type="button" aria-label="Good answer" className={m.feedback === 1 ? 'on' : ''} onClick={() => onFeedback(m, 1)}>👍</button>
              <button type="button" aria-label="Bad answer" className={m.feedback === -1 ? 'on' : ''} onClick={() => onFeedback(m, -1)}>👎</button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export function Chat({ state }: { state: MatchState | null }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [think, setThink] = useState(false)
  const [busy, setBusy] = useState(false)
  const [cooldown, setCooldown] = useState(0)
  const [chips, setChips] = useState<string[]>([])
  const [showTranscript, setShowTranscript] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const version = state?.version ?? 0
  useEffect(() => {
    // Refresh suggestions every few balls, not every ball.
    if (version % 3 !== 0 && chips.length) return
    fetchSuggestions().then(setChips).catch(() => {})
  }, [version]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (cooldown <= 0) return
    const t = window.setTimeout(() => setCooldown(c => c - 1), 1000)
    return () => window.clearTimeout(t)
  }, [cooldown])

  const patch = (id: string, fn: (m: ChatMessage) => ChatMessage) =>
    setMessages(ms => ms.map(m => (m.id === id ? fn(m) : m)))

  const ask = useCallback(async (question: string) => {
    const q = question.trim()
    if (!q || busy || cooldown > 0) return
    const botId = uid()
    setMessages(ms => [...ms, { id: uid(), role: 'user', text: q }, { id: botId, role: 'bot', text: '', pending: true, tools: [] }])
    setInput('')
    setBusy(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      for await (const ev of chatStream(q, think, ctrl.signal)) {
        if (ev.type === 'token') patch(botId, m => ({ ...m, text: m.text + (ev.text as string) }))
        else if (ev.type === 'thinking') patch(botId, m => ({ ...m, thinking: (m.thinking ?? '') + (ev.text as string) }))
        else if (ev.type === 'tool') patch(botId, m => ({ ...m, tools: [...(m.tools ?? []), ev as unknown as ToolEvent] }))
        else if (ev.type === 'meta') patch(botId, m => ({ ...m, model: ev.model as string | null }))
        else if (ev.type === 'route') patch(botId, m => ({ ...m, route: ev as unknown as RouteEvent }))
        else if (ev.type === 'agent') patch(botId, m => ({ ...m, agents: [...(m.agents ?? []), ev as unknown as AgentEvent] }))
        else if (ev.type === 'done') {
          const done = ev as unknown as DoneEvent
          patch(botId, m => ({ ...m, text: done.answer, done, pending: false }))
          if (done.retry_after) setCooldown(Math.ceil(done.retry_after))
        } else if (ev.type === 'error') patch(botId, m => ({ ...m, error: ev.message as string, pending: false }))
      }
    } catch (e) {
      if (e instanceof RateLimitError) {
        setCooldown(Math.ceil(e.retryAfter))
        patch(botId, m => ({ ...m, pending: false, error: `You're asking faster than the limit allows. Try again in ${Math.ceil(e.retryAfter)}s.` }))
      } else if ((e as Error).name === 'AbortError') {
        patch(botId, m => ({ ...m, pending: false, error: m.text ? undefined : 'Stopped.' }))
      } else {
        patch(botId, m => ({ ...m, pending: false, error: `Couldn't reach the chatbot. ${(e as Error).message}` }))
      }
    } finally {
      patch(botId, m => ({ ...m, pending: false }))
      setBusy(false)
      abortRef.current = null
    }
  }, [busy, cooldown, think])

  const onFeedback = (m: ChatMessage, r: 1 | -1) => {
    patch(m.id, x => ({ ...x, feedback: r }))
    sendFeedback(m.id, r).catch(() => {})
  }

  const reset = async () => {
    abortRef.current?.abort()
    setMessages([])
    await clearHistory().catch(() => {})
  }

  return (
    <section className="chat" aria-label="Chat">
      <header className="chat-head">
        <div>
          <h2>Ask Crickbot</h2>
          <p className="dim">Live score, players, IPL 2008–2017 history and the rules. Figures come from the feed and the record book.</p>
        </div>
        <div className="row">
          <button type="button" className="ghost" onClick={() => setShowTranscript(true)} title="See this session's full log: routes, agents, tool calls">Transcript</button>
          {messages.length > 0 && <button type="button" className="ghost" onClick={reset}>New chat</button>}
        </div>
      </header>

      <div className="messages" ref={listRef} aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <p>Try one of these, or ask your own question.</p>
          </div>
        )}
        {messages.map(m => <Message key={m.id} m={m} onFeedback={onFeedback} />)}
      </div>

      <div className="chips" role="list">
        {chips.map(c => (
          <button key={c} type="button" role="listitem" className="chip" disabled={busy || cooldown > 0} onClick={() => ask(c)}>{c}</button>
        ))}
      </div>

      <form className="composer" onSubmit={e => { e.preventDefault(); ask(input) }}>
        <label className="sr-only" htmlFor="question">Your question</label>
        <input id="question" value={input} maxLength={500} autoComplete="off" placeholder={cooldown > 0 ? `Slow down, ready in ${cooldown}s` : 'Ask about the match…'}
          onChange={e => setInput(e.target.value)} disabled={cooldown > 0} />
        <label className="think-toggle" title="Uses a reasoning model. Slower, shows its thinking.">
          <input id="think" type="checkbox" checked={think} onChange={e => setThink(e.target.checked)} /> Deep think
        </label>
        {busy
          ? <button type="button" className="stop" onClick={() => abortRef.current?.abort()}>Stop</button>
          : <button type="submit" disabled={!input.trim() || cooldown > 0}>Ask</button>}
      </form>
      {showTranscript && <TranscriptViewer onClose={() => setShowTranscript(false)} />}
    </section>
  )
}
