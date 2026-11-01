# Crickbot: live cricket commentary chatbot

A multi-agent RAG chatbot that answers fans' questions about a live IPL match (replayed ball by ball) and ten seasons of
IPL history. It runs entirely on a laptop CPU with local models, and is built like a production service: guardrails,
rate limiting and load shedding, graceful degradation, observability, an evaluation suite, and a human-in-the-loop
security review.

![Crickbot: live scoreboard, ball-by-ball feed and chat with sources](docs/whatsapp/1-chat-working.png)

| | |
|---|---|
| **Agents** | Strands Agents: Router → Live / Stats analyst / Rules expert → Commentator. Facts come from tools; the LLM only words them, and a validator removes any number, label or comparison the data doesn't support |
| **RAG** | Chroma + `nomic-embed-text`, hybrid search (vector + BM25, rank fusion), 827 documents chunked by meaning; measured recall@k, groundedness per claim, and an LLM judge calibrated against human labels |
| **Stack** | FastAPI, React + TypeScript, Ollama (`qwen2.5:3b`, `qwen3:1.7b`), SQLite, Chroma |
| **Reliability** | Per-session/IP rate limits, bounded LLM queue with load shedding, circuit breakers, degrade levels, chaos switches |
| **Security** | Prompt-injection guard (leetspeak, look-alikes, Hindi), streaming output checks, PII redaction, signed sessions, fail-closed secrets, a guarded security-reviewer agent with a human approval console |
| **Observability** | Prometheus metrics, OpenTelemetry traces (Jaeger), JSON logs with correlation IDs, a per-agent dashboard (runs, latency, tokens, tool calls, handoffs) |
| **Tests** | 217 automated tests with a fake LLM, plus answer, resilience, fuzz and RAG-quality checks against the real models |

![Architecture: containers](docs/whatsapp/3-architecture.png)

Everything runs on your machine: Ollama for the LLM and embeddings, Chroma for vectors, SQLite for stats.
Data: the public IPL 2008-2017 dataset (`matches.csv`, `deliveries.csv`, as published on Kaggle), in `Cricket-data/`.

- **Live match**: the 2017 IPL final (MI vs RPS, won by 1 run) replays from `Cricket-data/deliveries.csv`, one ball every 4 s.
- **Questions**: score, chase maths, players, head-to-head, seasons, rules (DRS, LBW, free hit, DLS...).
- **History**: IPL 2008-2017, 636 matches, 150K deliveries, 827 indexed documents.

## Run it

```bash
scripts/setup.sh     # once: pulls models, installs deps, writes backend/.env secrets, builds DB + vectors + UI
scripts/start.sh     # starts Ollama if needed, then the app
```

Open http://127.0.0.1:8000. The admin token for the **System** tab is in `backend/.env` (`CRICKET_ADMIN_TOKEN`).

| Command | What it does |
|---|---|
| `python3 scripts/ask.py "Who won IPL 2010?" [--think]` | Ask from the terminal, shows tools, path and timing |
| `python3 scripts/resilience_check.py` | Breaks each component in turn and checks the app degrades instead of failing |
| `cd backend && uv run pytest` | 217 tests in ~10 s, no Ollama needed: unit tests plus `tests/test_edge_cases.py`, which runs the whole app with a fake LLM against every input that broke it |
| `python3 scripts/answer_eval.py` | 22 questions checked against facts read straight from the database (real model) |
| `cd backend && uv run python -m cricket_chatbot.rag.quality` | RAG + LLM quality: recall@k, context precision, answer facts, groundedness per claim, retrieval-first triage of bad answers, latency per stage (p50/p95). Calibrates the LLM judge against human labels first. `--no-judge` for a fast run (~2 min), `--calibrate-only` for the judge alone |
| `python3 scripts/fuzz_check.py` | Throws ~110 hostile inputs at the running app and reports any that break it |
| `scripts/dev.sh` | Frontend with hot reload on :5173 (proxies to :8000) |
| `cd backend && uv run python -m cricket_chatbot.ingest` | Re-index after changing data or `knowledge/cricket_rules.md` (only changed docs are re-embedded) |

## Multi-agent answers (Strands Agents)

Questions that need the AI model go through a multi-agent system built on [Strands Agents](https://strandsagents.com) (`backend/src/cricket_chatbot/agents/`):

| Agent | Job | Tools |
|---|---|---|
| **Router** | Which desks does the question need: live, stats, rules (one or more)? Rules decide first; a small LLM call only when no rule fires. | none |
| **Live** | The match being played now | `get_live_score`, `get_ball_by_ball`, `win_probability`, `get_weather` (mock), `get_pitch_report` (mock) |
| **Stats analyst** | IPL history 2008-2017 | `query_player_stats`, `team_profile`, `phase_stats`, `batter_vs_bowler`, `dismissal_breakdown`, `venue_stats`, `toss_impact`, `find_matches`, `player_of_match_awards`, `head_to_head`, `season_summary`, `team_season_record`, `team_titles`, `top_players`, `milestone` |
| **Rules expert** | Laws of the game | `search_rules` (vector search over the rule book) |
| **Commentator** | Writes the answer from the desks' reports | none |

How it stays fast and correct on a CPU-only 3B model:
- **The planner makes the obvious tool calls deterministically** ("Malinga at the death" → `phase_stats`). The Stats analyst's own LLM picks tools only when the planner can't, capped at one turn (choose, run, stop).
- **Desks hand the Commentator raw tool results**, not a model's paraphrase. When a specific analysis ran, the generic career record is left out.
- **Comparisons are worked out in code.** A sentence that credits the better figure to the wrong player is removed.
- **The validator removes any sentence** whose numbers, decimals or stat counts aren't in what the agents fetched.
- **Every agent uses the app's `OllamaClient`** (`agents/model.py`, a Strands model provider), so circuit breakers, failure injection, metrics and traces apply to agents too.
- **Fast paths stay in front:** templates, cache, database facts and the rule book answer most questions in ~200 ms without any agent.

Mock tools return values marked `MOCK DATA`, and the Commentator must say they are simulated.

## Agent observability

Every agent execution is recorded, without changing what the agents do. The recorder swallows its own errors, and a test proves that a broken recorder never breaks an answer.

- **Agents recorded:** Crickbot (the whole turn), Planner, Router, Live, Stats analyst, Rules expert, Commentator.
- **Per run:** agent name, run ID and parent run ID, correlation ID (the request's `X-Request-ID`), OpenTelemetry trace ID, and start/end timestamps.
- **What happened:**
  - input and output summaries (redacted);
  - every tool call (arguments, ok/error, ms);
  - LLM calls, models, and tokens in and out;
  - retries, latency, status (ok, error, degraded, blocked, cancelled), error text, and which agent handed over.

| Where | What |
|---|---|
| **Agents tab** (`#agents`, admin token) | The observability dashboard, all from real recorded data (nothing mocked). See the list below the table. |
| `backend/var/app.log` | One JSON line per agent run (`"msg": "agent run"`), alongside all other logs. Rotates at 10 MB × 5. |
| `/metrics` | `agent_runs_total{agent,status}`, `agent_run_seconds{agent}`, `agent_tokens_total{agent,direction}`, `agent_run_tool_calls_total{agent,tool,outcome}`, `agent_handoffs_total{from_agent,to_agent}`, `agent_retries_total{agent}` |
| `backend/var/metrics/agent_runs_<date>.csv` | One row per agent run |
| `backend/var/metrics/tool_calls_<date>.csv` | One row per tool call, with its run, correlation and trace IDs |
| `backend/var/metrics/agent_metrics_<date>.csv` | Every minute, one row per agent: runs, successes, errors, success rate, p50/p95/max latency, tokens, LLM calls, tool calls, retries |

The Agents tab includes:
- **Toolbar:** a time range (15 min to 24 h), an agent filter and auto-refresh (off, 5, 15 or 30 s).
- **KPI cards:** questions, success rate, errors, turn latency p50/p95, tokens, LLM cost, tool calls and failure rate, retries and handoffs, runs in progress.
- **Time-series charts,** each with a hover/keyboard crosshair and a table view: runs by outcome, latency, tokens in/out, tool calls by result.
- **Agent status cards:** healthy, degraded, failing or idle, based on the last 5 minutes.
- **Handoff graph** between agents.
- **Grouped errors and a tool table.**
- **Executions explorer:** search by run, correlation or trace ID, or question text.
- **Live log stream** from `app.log`, filterable by level and text; dashboard polling is hidden by default.
- **CSV downloads.**
- **Drill-down for any execution:** the agent timeline, each run's input, output, LLM usage and tool calls with arguments and errors, its trace waterfall, and every log line with its correlation ID.

The dashboard shows the last 24 h, reloaded from the CSVs after a restart.

**LLM cost:** local Ollama has no per-token charge, so cost shows $0. To estimate what a hosted model would cost for the same traffic, set `CRICKET_LLM_PRICE_IN_PER_MTOK` and `CRICKET_LLM_PRICE_OUT_PER_MTOK` (USD per million tokens).

**Admin API:** `/api/admin/agents/{summary,timeseries,tools,errors,runs,runs/{id},workflow/{correlation_id},logs,csv}`, with `?range=15m|1h|6h|24h` and `&agent=`.

**Redaction:** API keys, passwords, bearer tokens, common key formats (`sk-…`, `AKIA…`, JWTs), the app's admin token and session secret, emails and phone numbers are redacted from every log line, CSV row and dashboard field.

**Retention:** CSVs are kept for 30 days. The dashboard's in-memory buffer holds the latest 3,000 runs.

## Conversation transcripts

Every chat session writes one `.txt` file to `backend/var/conversations/`, named `<start time>_<hashed session>.txt`. Each turn records:
- the question, PII-redacted;
- the router's decision and method;
- each agent with its timing;
- every tool call with arguments, timing and result;
- the answer, the validator's removals and the sources.

- **Chat → Transcript** shows your own session's file. It's authorised by your session token, so you can't read anyone else's.
- **Dashboard → Conversation logs** lists and opens every session's file. It's admin token only, and file names are validated, so nothing outside the folder can be read.
- **Limits:** a file stops growing at 2 MB, and only the newest 500 files are kept.

## Evaluating answer quality

`uv run python -m cricket_chatbot.rag.quality` runs the real agent over `knowledge/answer_quality_eval.json` and writes a report to `backend/var/eval/`.

1. **Calibrate the judge before trusting it.** The grader (exact number match, then an LLM judge) is scored against hand-labelled claims in `knowledge/judge_calibration.json`. It must reach 85% agreement and kappa 0.7, on the whole set and on the half never inspected while tuning the judge prompt. Otherwise its verdicts are advisory and never fail an answer.
2. **Retrieval:** recall@1/3/5 and MRR against labelled chunks, and the precision of the chunks the agent actually used.
3. **Answers:** required facts present, known-wrong strings absent.
4. **Groundedness:** every sentence checked against the evidence the agent had (the agent reports it internally; the HTTP API strips it).
5. **Check retrieval first:** a bad answer is a *retrieval miss* if the needed source never reached the agent, otherwise a *generation error*.
6. **Latency per stage** from the OpenTelemetry spans: embed, search, tools, LLM, other, total.

The calibration labels were drafted by Claude: review them and set `"reviewed": true` before relying on the judge.

## Architecture

```
Browser (React)  ──SSE──▶  FastAPI ─┬─ security middleware: request id, CSP + headers, body limit, metrics, JSON logs
  scoreboard ◀── ticker SSE ────────┤  auth: signed anonymous session token · admin token for ops
  chat       ──▶ /api/chat ─────────┤  rate limits: per fan · per IP · global (token buckets)
  system tab ──▶ /api/ops, /admin   │
                                    ▼
                         Agent pipeline (agent/agent.py)
   1 guard        injection, betting, abuse/hate, PII redaction, length
   2 template     "score?", "who's batting?", "target?" answered from live state, no LLM
   3 cache        key = state version + normalised question (live) or "hist" (history)
   4 coalesce     identical in-flight questions share one LLM call
   5 admission    1 LLM slot, 6 queued, 45 s wait, beyond that: shed to a fast data answer
   6 planner      runs obvious stats tools up front (players, seasons, head-to-head, leaderboards)
   7 retrieval    Chroma + nomic-embed-text over rules, seasons, teams, players, matches
   8 LLM loop     Ollama qwen2.5:3b (tools) or qwen3:1.7b (Deep think, streams reasoning), up to 3 tool rounds
   9 validator    every number in the answer must appear in the state, tool results or notes
  10 output guard prompt-leak and abuse check
                                    │
   Live feed replay ─▶ StateBuilder (single writer, versioned, idempotent) ─▶ broadcaster (bounded queues)
```

### Graceful degradation

Each dependency has its own circuit breaker, and a background probe closes it again when the dependency recovers.

| Component down | What fans get |
|---|---|
| Chat model (Ollama) | Level 2: templates, cached answers, record-book search and the live score. Ticker unaffected. |
| Embeddings or Chroma | Agent loses knowledge search, keeps stats tools. |
| Stats DB | Agent loses stats tools, keeps knowledge search. |
| Live feed | State freezes; the scoreboard shows "Feed delayed Ns". |
| Too many questions | Level 1 at half-full queue (thinking off, shorter answers), then requests are shed to a quick data answer. |
| Ticker stream refused or dropping | Browser falls back to ETag polling of `/api/live` every 2 s. |

Use the **System** tab (after unlocking with the admin token) to inject a failure into any component, force a level, pause or speed up the feed, and look at traces.

### Security

- Binds to 127.0.0.1. CORS is limited to the local UI origins.
- Signed (HMAC-SHA256), expiring, anonymous session tokens. Admin endpoints need `X-Admin-Token` (constant-time compare).
- Rate limits per session, per IP, globally, and on session creation. Request body limit 16 KB; questions limited to 500 characters.
- Prompt injection: fixed system prompt, fan text fenced in `<fan_question>` tags (tags stripped from input), injection patterns blocked, read-only tools with validated arguments, read-only SQLite connection, no free-form SQL, output checked for prompt leaks.
- Content: betting/fantasy/fixing requests refused; abuse and hate blocked on input and output.
- Privacy: emails, phone numbers and bearer tokens are redacted before reaching the model or the logs. Chat memory is in-process, bounded, and expires after 1 h.
- Headers: strict CSP (no inline or eval scripts), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store` on the API.
- Secrets live in `backend/.env` (mode 600, git-ignored) and are generated by `setup.sh`.

### Observability

- **Logs**: JSON to stdout, one line per request with `request_id` (also returned as `X-Request-ID`), PII redacted.
- **Metrics**: Prometheus at `/metrics`: answers by path, latency and first-token histograms, LLM tokens, tool calls, rate-limit and shed counts, breaker state, component health, degrade level, feed lag, ticker clients.
- **Traces**: OpenTelemetry spans for the chat pipeline, Ollama calls, retrieval and every tool. The last 50 chat traces show as a waterfall in the System tab. Set `CRICKET_OTLP_ENDPOINT` to ship them to Jaeger or Tempo (`ops/docker-compose.observability.yml`, optional, not exercised here).
- **Health**: `/health/live`, `/health/ready` (component detail), `/api/ops/status`.

## Dashboard and traffic tests

Open http://127.0.0.1:8000/#ops (the **Dashboard** tab). It refreshes every 2 s:

- **Status banner**: healthy, degraded or down, with the active alerts and how long each has been firing.
- **12 charts, last 10 minutes**: answers, AI answers, rate-limited, throttled, shed, 5xx, LLM queue, p95 answer time, feed lag, cache hit rate, components down, service level. A chart gets an amber border while it needs attention.
- **Alert history**: every alert that fired and resolved. Alerts also go to the JSON log as `ALERT fired` / `ALERT resolved`.
- **Right panel** (unlock with `CRICKET_ADMIN_TOKEN`): break or restore any component, force a service level, control the feed, view traces.
- **Test lab**: one-click tests with a pass/fail verdict. **Live limits**: change limits without a restart.

| Mechanism | What it protects | How to test |
|---|---|---|
| Rate limiting | One fan flooding the bot: 20 questions/min per session, burst 6 (per IP: 3×) | Test lab → Rate limiting, or `python3 scripts/traffic_test.py rate_limit` |
| Throttling | Total capacity: 10 requests/s globally, across all fans | Test lab → Throttling, or `python3 scripts/traffic_test.py throttle` |
| Load shedding | The LLM: 1 running, 6 queued, 45 s wait; extra questions get a fast data answer | Test lab → Load shedding, or `python3 scripts/traffic_test.py load_shed` |

Run all three with `python3 scripts/traffic_test.py`. The throttle and load-shed tests lower the limits for a moment and restore them afterwards.

By hand, with curl (expect 6 × 200, then 429):

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/session | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
for i in $(seq 1 10); do curl -s -o /dev/null -w "request $i -> HTTP %{http_code}\n" -X POST localhost:8000/api/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"question":"score?"}'; done
```

## Configuration

All settings are in `backend/src/cricket_chatbot/config.py` and can be overridden with `CRICKET_*` variables in `backend/.env`, for example:

```
CRICKET_CHAT_MODEL=qwen2.5:7b         # better answers if you have the RAM
CRICKET_THINK_MODEL=qwen3:4b
CRICKET_REPLAY_MATCH_ID=1             # any id from matches.csv
CRICKET_REPLAY_BALL_INTERVAL_S=2
CRICKET_RATE_PER_CLIENT_PER_MIN=20
CRICKET_LLM_MAX_CONCURRENCY=1         # raise only with a GPU and OLLAMA_NUM_PARALLEL
```

## Known limits

- On this CPU-only machine an AI answer takes about 4-15 s to the first word, and Deep think about 40 s. Templates and cache hits are instant.
- Small models (3B and 1.7B) make mistakes on multi-step questions. The planner and validator catch many wrong figures, not all; an unverified figure is flagged in the UI.
- Sessions, cache and rate limits are in memory, so they reset on restart and don't scale past one process. For more instances, move them to Redis. The production design is in `docs/architecture.html`.
