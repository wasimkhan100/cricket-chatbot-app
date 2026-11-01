# Prompt: build a production-style cricket commentary chatbot (local)

Act as a senior software architect. Design, build, test and document a cricket commentary chatbot that runs entirely on my local machine. Work in phases, verify each phase with real measurements before moving on, and tell me honestly what works, what doesn't, and what you couldn't test.

## Context

- Workshop brief: fans ask questions about a live match; a ball-by-ball feed updates every few seconds; for an India vs Pakistan final, 5 million fans arrive in 10 minutes.
- Data: `Cricket-data/matches.csv` and `Cricket-data/deliveries.csv` (IPL, 10 seasons, 2008–2017: 636 matches, ~150K deliveries).
- Machine: Ubuntu, CPU only (no GPU), ~16 GB RAM. Ollama is installed. Use local models only: `qwen2.5:3b` (chat with tools), `qwen3:1.7b` (optional "deep think" mode with visible reasoning), `nomic-embed-text` (embeddings). You may start `ollama serve` and pull models yourself.
- Keep RAM in mind: don't keep all three models loaded at once (unload the thinking model a few minutes after use).

## Phase 1 · Design (before writing code)

Produce a design document with:
1. Functional and non-functional requirements (scale, freshness, latency, availability, accuracy, cost, safety, privacy).
2. A high-level architecture diagram.
3. A capacity estimate for 5M fans (questions/s, LLM calls/s, connections, storage).
4. A token cost estimate per match, comparing a naive "every question to the LLM" design with an optimised one.
Cover scalability, security, maintainability and failure handling.

## Phase 2 · Build the application

- **Frontend:** React + TypeScript. A live scoreboard and ball-by-ball commentary, a chat with streamed answers, and a separate Dashboard tab. Each answer shows where it came from (badge), how long it took, which ball it was based on, the tools used, and its sources.
- **Backend:** Python FastAPI agent with server-sent events for chat and for the live ticker.
- **Live match:** replay a real match from the data ball by ball (e.g. the 2017 IPL final), with a single writer, versioned state, and duplicate-safe updates.
- **Data:** build a read-only SQLite stats database from the CSVs. The LLM must never write SQL; it may only call fixed, parameterised tools (player stats, head-to-head, season summary, team season record, team titles, milestones like "2000+ runs", leaderboards, live scorecard, knowledge search).
- **Vector database:** ingest into Chroma with Ollama embeddings. Chunk by meaning, not by size: one document per match, season, team, player and rule section, plus a cricket-rules file. Re-ingestion must only re-embed changed documents.
- **Agent:** retrieval + tools + LLM, with optional thinking mode. Facts must come from the database; the model only words them.
- **Run scripts:** setup (models, deps, secrets in `.env`, DBs, UI build) and start. Bind to localhost only.

## Phase 3 · Reliability, traffic control and security

- Rate limiting (per session and per IP), global throttling, and load shedding for the LLM (bounded concurrency, bounded queue, bounded wait; beyond that, answer quickly from data instead of timing out).
- Circuit breakers per dependency, background health probes, and degrade levels (normal / busy / AI offline). If any component fails, the rest must keep working: never a total outage.
- Guardrails: prompt-injection detection (including leetspeak, look-alike Unicode letters, spaced letters, Hindi/Hinglish phrasing), betting/abuse/hate filtering, PII redaction, output checks so the model can never reveal its system prompt (check while streaming, not only after), and a validator that removes sentences containing numbers not found in the data.
- Scope and topic handling: honest replies for questions outside the data (other seasons, World Cup, ODIs), polite refusal for non-cricket questions, and "please ask a full question" for content-free input.
- Security headers, signed session tokens, an admin token for operator actions, and request body limits.

## Phase 4 · Observability

- JSON logs with request IDs, Prometheus metrics at `/metrics`, OpenTelemetry traces, `/health/live` and `/health/ready`.
- A built-in observability dashboard: status banner, live charts (last 10 minutes), alert rules with fired/resolved history, component health with Break/Restore buttons for failure injection, operator controls (pause/speed/restart the match, clear cache, force degrade level), recent traces and errors, live-tunable limits, and a traffic test lab that runs rate-limit, throttle and load-shed tests with a pass/fail verdict.
- The admin unlock must accept a pasted `.env` line, verify the token immediately, and explain mistakes clearly.

## Phase 5 · Latency

- Measure where the time goes, stage by stage. Aim for ~500 ms answers: answer most questions without the LLM (templates for the live score, answers composed straight from database results, rule-book text, cached answers, scope replies), and keep the LLM only for "why / compare / explain" questions. Keep the system prompt stable so the model's prompt cache is reused.
- Report honestly what is achievable on a CPU and what would need a GPU or hosted model.

## Phase 6 · Prove it works

- Improve vector search (season filter, BM25 keywords, question-type ranking, rank fusion, diversity) and measure hit@1 against plain vector search on a tuning set **and a held-out set you never tune against**.
- An end-to-end answer check: ask the running app questions and compare answers with facts read directly from the database, including checks that fail on known-false claims.
- Act as an attacker: fuzz the running app with ~100 hostile or unusual inputs (malformed JSON, huge or empty text, Unicode tricks, wrong types, bad tokens, bad admin values, NaN, disconnects, connection floods). Fix every break and pin each one with a test.
- An automated test suite that runs the whole app with a fake LLM (fast, no Ollama needed), plus scripts for resilience drills and traffic tests against the real app.
- Give me a senior-architect review: is it production ready, what's missing, how it behaves when something breaks, where to troubleshoot.

## Phase 7 · Presentation material

1. **Workshop PowerPoint:** architecture, C4 diagrams (context, containers, components), ADRs with trade-offs, request flow, data and chunking, retrieval results, security, failure handling, rate limiting / throttling / load shedding, latency and capacity, observability, testing, step-by-step run instructions, how to open both databases, a live demo script (questions that work, inputs it must refuse, failure drills), known weaknesses, covered vs pending, roadmap. Every slide gets speaker notes (say / do / avoid, with timing), plus a printable speaker script.
2. **"Explained simply" study deck:** the whole project in plain words with everyday comparisons, the problems we hit and how we fixed them, numbers to remember, a glossary, likely questions, and a 30-second pitch.
3. **Dashboard guide deck:** every panel, chart, alert rule, button and number on the dashboard, how each is calculated, and likely questions with answers.


## Phase 8 · Data walkthrough for colleagues

Show that the tables exist and that the embeddings were made in chunks:
- Run real queries on the SQLite stats database and on Chroma's own SQLite file, and capture the output as screenshots.
- Draw entity-relationship diagrams for both databases.
- Put everything in a deck: the pipeline from CSV to tables, chunks and vectors, plus a similarity search.

## Phase 9 · Evaluate RAG and the LLM

Measure each of these:
- recall@k and context precision: was the right chunk retrieved?
- relevance;
- groundedness per claim, backed by the sources;
- latency per stage (p50/p95).

Rules for the evaluation:
- **For a bad answer, check retrieval first:** label it a *retrieval miss* if the needed source never reached the model, otherwise a *generation error*.
- **Calibrate the LLM judge before trusting it,** against human-labelled claims, including the half you never tuned on. If it misses the bar, its verdicts are advisory only.
- **Show the results** as a readable HTML report and as WhatsApp-sized screenshots.

## Phase 10 · Multi-agent system with Strands Agents

- **Library:** convert the LLM calls to a multi-agent system using the Strands Agents library.
- **Router + orchestrator.** The router decides live, stats or rules (one or more), and the orchestrator runs the agents:
  - Live: `get_live_score`, `get_ball_by_ball`;
  - Stats analyst: `query_player_stats`;
  - Rules expert: `search_rules`;
  - Commentator: no tools; writes the answer from the other agents' output.
- **More tools from the real data** (CSV and RAG). Build mock tools, clearly labelled, only where no data exists.
- **Session transcripts:** store each session's full conversation history, every tool call and the other details in one folder, as `.txt` files with timestamps. Add a UI button to view them.

## Phase 11 · Observability for every agent

- **Capture, for every agent execution:**
  - agent name, run ID, input and output summaries, tool calls;
  - LLM/model usage, tokens, latency, errors, retries, handoffs between agents and status;
  - timestamps, correlation IDs and trace IDs, so a multi-agent workflow can be followed end to end.
- **Track metrics:** success/failure rate, latency, token usage and tool-call counts. Save the metrics as CSV files too.
- **Redact** API keys, credentials and sensitive data.
- **Observability must not change** agent behaviour or workflow logic.
- **Build an interactive observability dashboard** on the real data (no mock data):
  - KPI cards and time-series charts;
  - agent status, success/failure rates, latency, tokens, LLM cost, tool calls, errors, retries and handoffs;
  - searchable, filterable logs (by agent, run ID and status), error details, and a drill-down for each execution;
  - auto-refresh, clear loading/empty/error states, and a clean, responsive layout.
- **Explain the dashboard in a deck** with everyday examples, so that anyone can read every panel correctly.

## Ground rules

- Use real numbers you measured; never invent figures. If a number in the slides doesn't match the code, fix the slides.
- Verify in a real browser, not just with unit tests.
- When something can't be done (e.g. sudo, Docker not running), say so and offer the next best option.
- Don't put secrets (admin token, session secret) in slides, screenshots or shared files.

## Lessons from the first build (avoid these bugs)

- A small model mixes up loosely related notes: run the obvious database lookups first and give the model readable results.
- Name matching must use whole words ("T Kohli" matched inside "Virat Kohli").
- Follow-up questions should borrow the subject from the previous question, not its season.
- Plain vector search can't tell one match from 60 similar ones: label special documents (finals) and use hybrid search.
- The phone-number privacy filter must not swallow lists of years ("2008 2009 2010").
- Validation errors must not echo NaN back into JSON (that crashed the error handler).
- Loading three models at once froze a 16 GB laptop.
- A 0-20 "small number" allowance let invented counts through ("14 matches, won 7"): stats must come from the data, and only from the parts of the live scorecard the model actually saw.
- A 3B model compares numbers backwards (129.9 "beats" 130.9): work comparisons out in code and remove sentences that contradict them.
- Give agents raw tool results, not a model's summary of them, and leave out generic records when a specific analysis exists (a batting strike rate got used to explain bowling).
- Redaction patterns must not touch IDs ("5b8765432109edd5" became "5b[phone]edd5").
- Don't attach tracing contexts across streaming yields: it logs false "Failed to detach context" errors.
