import { useEffect, useState } from 'react'
import { subscribeLive } from './api'
import { Chat } from './components/Chat'
import { AgentsDashboard } from './components/AgentsDashboard'
import { Dashboard } from './components/Dashboard'
import { CommentaryFeed, Scoreboard } from './components/Scoreboard'
import type { MatchState } from './types'

type Tab = 'match' | 'ops' | 'agents'

export default function App() {
  const [state, setState] = useState<MatchState | null>(null)
  const [link, setLink] = useState<'stream' | 'polling' | 'offline'>('stream')
  const [tab, setTab] = useState<Tab>(() => (window.location.hash === '#ops' ? 'ops' : window.location.hash === '#agents' ? 'agents' : 'match'))

  useEffect(() => subscribeLive(setState, setLink), [])
  useEffect(() => { window.location.hash = tab === 'match' ? '' : tab }, [tab])

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-ball" aria-hidden="true" />
          <span>Crickbot</span>
          {state && <span className="brand-match">{state.title}</span>}
        </div>
        <nav className="tabs" aria-label="Views">
          <button type="button" className={tab === 'match' ? 'on' : ''} onClick={() => setTab('match')}>Match</button>
          <button type="button" className={tab === 'ops' ? 'on' : ''} onClick={() => setTab('ops')}>Dashboard</button>
          <button type="button" className={tab === 'agents' ? 'on' : ''} onClick={() => setTab('agents')}>Agents</button>
        </nav>
      </header>

      {tab === 'agents' ? <AgentsDashboard /> : tab === 'ops' ? <Dashboard /> : (
        <main className="layout">
          <div className="col-match">
            <Scoreboard state={state} link={link} />
            <CommentaryFeed state={state} />
          </div>
          <Chat state={state} />
        </main>
      )}
    </div>
  )
}
