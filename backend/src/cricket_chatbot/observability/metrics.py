"""Prometheus metrics. Scrape GET /metrics."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_seconds", "HTTP latency", ["route"], buckets=(0.01, 0.05, 0.1, 0.3, 1, 3, 10, 30, 90))

QUESTIONS = Counter("chat_questions_total", "Questions by answer path", ["path"])  # template|cache|llm|degraded|blocked|shed
ANSWER_LATENCY = Histogram("chat_answer_seconds", "End-to-end answer time", ["path"], buckets=(0.05, 0.1, 0.3, 1, 3, 10, 30, 60, 120))
FIRST_TOKEN = Histogram("chat_first_token_seconds", "Time to first streamed token", buckets=(0.1, 0.3, 1, 2, 5, 10, 30, 60))

LLM_CALLS = Counter("llm_calls_total", "Ollama calls", ["model", "kind", "outcome"])
LLM_TOKENS = Counter("llm_tokens_total", "Tokens reported by Ollama", ["model", "direction"])
LLM_INFLIGHT = Gauge("llm_inflight", "LLM calls currently running")
LLM_QUEUE = Gauge("llm_queue_depth", "Requests waiting for an LLM slot")

TOOL_CALLS = Counter("agent_tool_calls_total", "Agent tool calls", ["tool", "outcome"])
RAG_HITS = Histogram("rag_results", "Documents returned by retrieval", buckets=(0, 1, 2, 3, 4, 5, 8))

RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by rate limits", ["scope"])
SHED = Counter("load_shed_total", "Requests shed by admission control", ["reason"])
GUARD_BLOCKS = Counter("guard_blocks_total", "Questions or answers blocked by safety checks", ["stage", "reason"])
VALIDATOR_FAILS = Counter("validator_mismatch_total", "LLM answers whose numbers disagreed with match state")

BREAKER_STATE = Gauge("circuit_breaker_open", "1 when the breaker is open", ["dependency"])
COMPONENT_UP = Gauge("component_up", "1 when a component is healthy", ["component"])
DEGRADE_LEVEL = Gauge("degrade_level", "Current degrade level (0 = normal)")

FEED_BALLS = Counter("feed_balls_total", "Deliveries applied to match state")
FEED_LAG = Gauge("feed_seconds_since_last_ball", "Seconds since the last delivery was applied")
SSE_CLIENTS = Gauge("ticker_clients", "Open live ticker streams")

# Per agent (multi-agent path): see observability/agent_runs.py
AGENT_RUNS = Counter("agent_runs_total", "Agent executions by status", ["agent", "status"])
AGENT_LATENCY = Histogram("agent_run_seconds", "Agent execution time", ["agent"], buckets=(0.001, 0.01, 0.05, 0.2, 1, 3, 10, 30, 90))
AGENT_TOKENS = Counter("agent_tokens_total", "LLM tokens used by each agent", ["agent", "direction"])
AGENT_TOOL_CALLS = Counter("agent_run_tool_calls_total", "Tool calls made by each agent", ["agent", "tool", "outcome"])
AGENT_HANDOFFS = Counter("agent_handoffs_total", "Work handed from one agent to the next", ["from_agent", "to_agent"])
AGENT_RETRIES = Counter("agent_retries_total", "LLM calls retried after a failure within one agent run", ["agent"])
