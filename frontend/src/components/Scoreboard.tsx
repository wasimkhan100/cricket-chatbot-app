import { useEffect, useState } from 'react'
import type { MatchState } from '../types'

const short = (team: string) =>
  ({
    'Mumbai Indians': 'MI', 'Rising Pune Supergiant': 'RPS', 'Chennai Super Kings': 'CSK', 'Royal Challengers Bangalore': 'RCB',
    'Kolkata Knight Riders': 'KKR', 'Sunrisers Hyderabad': 'SRH', 'Delhi Daredevils': 'DD', 'Kings XI Punjab': 'KXIP',
    'Rajasthan Royals': 'RR', 'Deccan Chargers': 'DC', 'Gujarat Lions': 'GL', 'Pune Warriors': 'PWI', 'Kochi Tuskers Kerala': 'KTK',
  } as Record<string, string>)[team] ?? team.slice(0, 3).toUpperCase()

function ballClass(b: string) {
  if (b === 'W') return 'ball ball-w'
  if (b === '4' || b === '6') return 'ball ball-b'
  if (b.includes('wd') || b.includes('nb')) return 'ball ball-x'
  if (b === '0') return 'ball ball-dot'
  return 'ball'
}

export function Scoreboard({ state, link }: { state: MatchState | null; link: string }) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(t)
  }, [])

  if (!state) return <section className="board board-empty" aria-busy="true">Connecting to the live feed…</section>

  const inn = state.innings[state.innings.length - 1]
  const age = state.updated_at ? now - state.updated_at : 0
  const stale = state.status === 'live' && age > 20
  const striker = inn?.batters.find(b => b.name === state.striker)
  const nonStriker = inn?.batters.find(b => b.name === state.non_striker)
  const bowler = inn?.bowlers.find(b => b.name === state.bowler)

  return (
    <section className="board" aria-label="Live score">
      <div className="board-top">
        <span className="board-title">IPL {state.season} · {state.venue}</span>
        <span className={`live-pill ${state.status === 'complete' ? 'done' : stale ? 'stale' : ''}`}>
          {state.status === 'complete' ? 'Result' : stale ? `Feed delayed ${Math.round(age)}s` : 'Live'}
          <span className="link-mode" title="Ticker connection">{link}</span>
        </span>
      </div>

      <div className="innings-row">
        {[state.team1, state.team2].map(team => {
          const i = state.innings.find(x => x.batting_team === team)
          const batting = inn?.batting_team === team && state.status !== 'complete'
          return (
            <div key={team} className={`team ${batting ? 'team-batting' : ''}`}>
              <span className="team-code">{short(team)}</span>
              <span className="team-name">{team}</span>
              {i ? <span className="team-score">{i.runs}/{i.wickets}</span> : <span className="team-yet">Yet to bat</span>}
              {i && <span className="team-overs">{i.overs} ov · RR {i.run_rate.toFixed(2)}</span>}
            </div>
          )
        })}
      </div>

      <p className="equation">
        {state.status === 'complete' ? state.result
          : state.chase ? <>Need <b>{state.chase.runs_needed}</b> from <b>{state.chase.balls_left}</b> balls · RRR <b>{state.chase.required_run_rate.toFixed(2)}</b></>
          : state.innings.length ? `${state.toss}.` : 'Match about to start'}
      </p>

      {inn && state.status !== 'complete' && (
        <div className="crease">
          <div>
            <span className="label">Batting</span>
            {striker && <span className="mono">{striker.name}* {striker.runs} ({striker.balls})</span>}
            {nonStriker && <span className="mono dim">{nonStriker.name} {nonStriker.runs} ({nonStriker.balls})</span>}
          </div>
          <div>
            <span className="label">Bowling</span>
            {bowler && <span className="mono">{bowler.name} {bowler.overs}-{bowler.runs}-{bowler.wickets}</span>}
          </div>
        </div>
      )}

      <div className="last-balls" aria-label="Recent deliveries">
        {state.last_balls.map((b, i) => <span key={`${state.version}-${i}`} className={ballClass(b)}>{b}</span>)}
      </div>
      <p className="last-event">{state.last_event}</p>
    </section>
  )
}

export function CommentaryFeed({ state }: { state: MatchState | null }) {
  if (!state?.commentary.length) return null
  return (
    <section className="commentary" aria-label="Ball by ball commentary">
      <h2>Ball by ball</h2>
      <ol>
        {state.commentary.map(c => (
          <li key={`${c.version}-${c.kind}-${c.text.slice(0, 12)}`} className={`c-${c.kind}`}>
            {c.kind === 'ai' && <span className="ai-tag">AI</span>}
            {c.text}
          </li>
        ))}
      </ol>
    </section>
  )
}
