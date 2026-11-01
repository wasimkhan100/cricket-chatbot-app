"""Agent-level observability: one record per agent execution.

Every run gets a run id, the request's correlation id and the OpenTelemetry trace id, so a multi-agent workflow can be
followed end to end: Crickbot (the turn) -> Planner -> Router -> Live / Stats analyst / Rules expert -> Commentator.
A run records its input and output (summarised and redacted), every tool call, LLM calls, tokens, retries, latency,
errors, status and which agent handed over to it.

Records go to: a structured JSON log line, Prometheus metrics, an in-memory buffer for the dashboard, and daily CSV
files in var/metrics/ (agent_runs, tool_calls, and a per-agent metrics snapshot every minute).

Observation never changes behaviour: every recording step swallows its own errors, and the wrapped code's exceptions
pass through untouched.
"""

import calendar
import contextvars
import csv
import logging
import re
import statistics
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from opentelemetry import trace

from .logging import redact, request_id_var
from .metrics import AGENT_HANDOFFS, AGENT_LATENCY, AGENT_RETRIES, AGENT_RUNS, AGENT_TOKENS, AGENT_TOOL_CALLS
from .tracing import tracer

log = logging.getLogger("agent_runs")
SUMMARY_CHARS = 300
CSV_NAME = re.compile(r"^(agent_runs|tool_calls|agent_metrics)_\d{4}-\d{2}-\d{2}\.csv$")
KEEP_DAYS = 30

RUN_COLUMNS = ["started_utc", "ended_utc", "run_id", "parent_run_id", "correlation_id", "trace_id", "session", "agent", "status",
               "latency_ms", "llm_calls", "models", "tokens_in", "tokens_out", "tool_calls", "tool_errors", "retries",
               "handoff_from", "input_summary", "output_summary", "error"]
TOOL_COLUMNS = ["ts_utc", "run_id", "correlation_id", "trace_id", "agent", "tool", "ok", "ms", "args", "error"]
METRIC_COLUMNS = ["ts_utc", "agent", "runs", "ok", "errors", "other_status", "success_rate", "latency_p50_ms", "latency_p95_ms",
                  "latency_max_ms", "tokens_in", "tokens_out", "llm_calls", "tool_calls", "tool_errors", "retries"]

current_run: contextvars.ContextVar["AgentRun | None"] = contextvars.ContextVar("current_agent_run", default=None)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def summarise(value, n: int = SUMMARY_CHARS) -> str:
    text = redact(" ".join(str(value).split()))
    return text if len(text) <= n else text[:n] + " …"


@dataclass
class AgentRun:
    agent: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_run_id: str = ""
    correlation_id: str = "-"
    trace_id: str = ""
    session: str = ""
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    status: str = "running"            # ok | error | degraded | cancelled | blocked | running
    input_summary: str = ""
    output_summary: str = ""
    error: str = ""
    handoff_from: str = ""
    llm_calls: int = 0
    models: set = field(default_factory=set)
    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 0
    tool_calls: list = field(default_factory=list)
    _llm_failed: bool = False

    def cost(self, prices: tuple[float, float]) -> float:
        return self.tokens_in * prices[0] / 1e6 + self.tokens_out * prices[1] / 1e6

    @property
    def latency_ms(self) -> float:
        return round(((self.ended or time.time()) - self.started) * 1000, 1)

    def set_output(self, value) -> None:
        try:
            self.output_summary = summarise(value)
        except Exception:
            pass

    def as_dict(self, prices: tuple[float, float] = (0.0, 0.0)) -> dict:
        return {"cost_usd": round(self.cost(prices), 6), "run_id": self.run_id, "parent_run_id": self.parent_run_id, "correlation_id": self.correlation_id,
                "trace_id": self.trace_id, "session": self.session, "agent": self.agent, "status": self.status,
                "started": self.started, "ended": self.ended, "started_utc": _iso(self.started),
                "ended_utc": _iso(self.ended) if self.ended else "", "latency_ms": self.latency_ms,
                "llm_calls": self.llm_calls, "models": sorted(self.models), "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "retries": self.retries, "tool_calls": self.tool_calls, "tool_call_count": len(self.tool_calls),
                "tool_errors": sum(1 for t in self.tool_calls if not t["ok"]), "handoff_from": self.handoff_from,
                "input_summary": self.input_summary, "output_summary": self.output_summary, "error": self.error}


class AgentObserver:
    def __init__(self, folder: Path | None = None, max_runs: int = 3000):
        self.folder = folder
        self.runs: deque[AgentRun] = deque(maxlen=max_runs)
        self._lock = threading.Lock()
        self._csv_lock = threading.Lock()
        self.started = time.time()
        self.active: dict[str, AgentRun] = {}
        self.prices: tuple[float, float] = (0.0, 0.0)
        self.history_loaded = 0
        if folder:
            folder.mkdir(parents=True, exist_ok=True)

    def configure(self, folder: Path | None, prices: tuple[float, float] = (0.0, 0.0), history_hours: float = 24) -> None:
        self.folder = folder
        self.prices = prices
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            self._prune()
            self.load_history(history_hours)

    def load_history(self, hours: float) -> None:
        """Reload recent runs (and their tool calls) from the CSVs, so a restart doesn't empty the dashboard."""
        try:
            cutoff = time.time() - hours * 3600
            tools: dict[str, list] = {}
            days = {time.strftime("%Y-%m-%d", time.gmtime(t)) for t in (cutoff, time.time())}
            parse = lambda s: calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + float("0" + s[19:-1])
            for day in sorted(days):
                p = self.folder / f"tool_calls_{day}.csv"
                if p.exists():
                    with p.open(newline="") as f:
                        for row in csv.DictReader(f):
                            tools.setdefault(row["run_id"], []).append({
                                "tool": row["tool"], "args": row["args"], "ok": row["ok"] == "True", "ms": float(row["ms"] or 0),
                                "error": row["error"], "ts": parse(row["ts_utc"])})
            loaded: list[AgentRun] = []
            for day in sorted(days):
                p = self.folder / f"agent_runs_{day}.csv"
                if not p.exists():
                    continue
                with p.open(newline="") as f:
                    for row in csv.DictReader(f):
                        started = parse(row["started_utc"])
                        if started < cutoff:
                            continue
                        r = AgentRun(agent=row["agent"], run_id=row["run_id"], parent_run_id=row["parent_run_id"],
                                     correlation_id=row["correlation_id"], trace_id=row["trace_id"], session=row["session"],
                                     started=started, ended=parse(row["ended_utc"]) if row["ended_utc"] else started,
                                     status=row["status"], input_summary=row["input_summary"], output_summary=row["output_summary"],
                                     error=row["error"], handoff_from=row["handoff_from"], llm_calls=int(row["llm_calls"] or 0),
                                     models=set(filter(None, row["models"].split(";"))), tokens_in=int(row["tokens_in"] or 0),
                                     tokens_out=int(row["tokens_out"] or 0), retries=int(row["retries"] or 0),
                                     tool_calls=tools.get(row["run_id"], []))
                        loaded.append(r)
            with self._lock:
                known = {r.run_id for r in self.runs}
                for r in sorted(loaded, key=lambda r: r.started):
                    if r.run_id not in known:
                        self.runs.append(r)
                self.runs = deque(sorted(self.runs, key=lambda r: r.started), maxlen=self.runs.maxlen)
            self.history_loaded = len(loaded)
        except Exception:
            log.exception("could not reload agent history from CSV")

    # --- recording ------------------------------------------------------------------------
    @contextmanager
    def run(self, agent: str, input=None, *, handoff_from: str = "", session: str = ""):
        """Wrap one agent execution. Exceptions from the wrapped code propagate unchanged."""
        parent = current_run.get()
        r = AgentRun(agent=agent, parent_run_id=parent.run_id if parent else "",
                     correlation_id=(parent.correlation_id if parent else request_id_var.get()),
                     session=session or (parent.session if parent else ""), handoff_from=handoff_from)
        try:
            r.input_summary = summarise(input) if input is not None else ""
        except Exception:
            pass
        # Not made the "current" span: the Commentator's run spans streaming yields, and detaching an OpenTelemetry
        # context from a different point than it was attached logs "Failed to detach context" errors. The span still
        # joins the request's trace (same trace id) with its own timing; tool and LLM spans stay children of the turn.
        span = tracer().start_span(f"agent.{agent.lower().replace(' ', '_')}")
        try:
            span.set_attribute("agent.name", agent)
            span.set_attribute("agent.run_id", r.run_id)
            span.set_attribute("agent.correlation_id", r.correlation_id)
            r.trace_id = format(span.get_span_context().trace_id, "032x")
        except Exception:
            pass
        token = current_run.set(r)
        self.active[r.run_id] = r
        try:
            yield r
            if r.status == "running":
                r.status = "ok"
        except GeneratorExit:
            r.status = "cancelled"
            raise
        except BaseException as e:
            r.status = "cancelled" if type(e).__name__ == "CancelledError" else "error"
            r.error = summarise(f"{type(e).__name__}: {e}", 400)
            try:
                span.set_status(trace.Status(trace.StatusCode.ERROR, r.error))
            except Exception:
                pass
            raise
        finally:
            r.ended = time.time()
            self.active.pop(r.run_id, None)
            try:
                current_run.reset(token)
            except ValueError:
                current_run.set(parent)  # closed from another context (client disconnected mid-stream)
            try:
                span.set_attribute("agent.status", r.status)
                span.end()
            except Exception:
                pass
            self._record(r)

    def note_tool(self, name: str, args, ok: bool, ms: float, error: str = "") -> None:
        r = current_run.get()
        if r is None:
            return
        try:
            r.tool_calls.append({"tool": name, "args": summarise(args, 200), "ok": bool(ok), "ms": round(ms, 1),
                                 "error": summarise(error, 200) if error else "", "ts": time.time()})
        except Exception:
            pass

    def note_llm(self, model: str, tokens_in: int = 0, tokens_out: int = 0, ok: bool = True) -> None:
        r = current_run.get()
        if r is None:
            return
        try:
            r.llm_calls += 1
            r.models.add(model)
            r.tokens_in += int(tokens_in or 0)
            r.tokens_out += int(tokens_out or 0)
            if r._llm_failed:
                r.retries += 1  # a call after a failed call in the same run
            r._llm_failed = not ok
        except Exception:
            pass

    def _record(self, r: AgentRun) -> None:
        try:
            with self._lock:
                self.runs.append(r)
            AGENT_RUNS.labels(r.agent, r.status).inc()
            AGENT_LATENCY.labels(r.agent).observe(r.latency_ms / 1000)
            if r.tokens_in:
                AGENT_TOKENS.labels(r.agent, "in").inc(r.tokens_in)
            if r.tokens_out:
                AGENT_TOKENS.labels(r.agent, "out").inc(r.tokens_out)
            if r.retries:
                AGENT_RETRIES.labels(r.agent).inc(r.retries)
            for t in r.tool_calls:
                AGENT_TOOL_CALLS.labels(r.agent, t["tool"], "ok" if t["ok"] else "error").inc()
            if r.handoff_from:
                AGENT_HANDOFFS.labels(r.handoff_from, r.agent).inc()
            d = r.as_dict(self.prices)
            log.log(logging.WARNING if r.status == "error" else logging.INFO, "agent run",
                    extra={k: d[k] for k in ("run_id", "parent_run_id", "correlation_id", "trace_id", "agent", "status", "latency_ms",
                                             "llm_calls", "tokens_in", "tokens_out", "tool_call_count", "tool_errors", "retries",
                                             "handoff_from", "input_summary", "output_summary", "error")}
                    | {"models": ",".join(d["models"]), "tools": ",".join(t["tool"] for t in r.tool_calls)})
            self._write_csv(r, d)
        except Exception:
            log.exception("could not record an agent run")  # never disturb the answer

    # --- CSV ------------------------------------------------------------------------------
    def _csv_path(self, kind: str, ts: float) -> Path:
        return self.folder / f"{kind}_{time.strftime('%Y-%m-%d', time.gmtime(ts))}.csv"

    def _append(self, path: Path, columns: list[str], rows: list[dict]) -> None:
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)

    def _write_csv(self, r: AgentRun, d: dict) -> None:
        if not self.folder:
            return
        row = {**d, "models": ";".join(d["models"]), "tool_calls": d["tool_call_count"]}
        tools = [{"ts_utc": _iso(t["ts"]), "run_id": r.run_id, "correlation_id": r.correlation_id, "trace_id": r.trace_id,
                  "agent": r.agent, "tool": t["tool"], "ok": t["ok"], "ms": t["ms"], "args": t["args"], "error": t["error"]}
                 for t in r.tool_calls]
        with self._csv_lock:
            self._append(self._csv_path("agent_runs", r.started), RUN_COLUMNS, [row])
            if tools:
                self._append(self._csv_path("tool_calls", r.started), TOOL_COLUMNS, tools)

    def write_metrics_snapshot(self) -> None:
        """One row per agent with the metrics since the app started: called every minute by the monitor."""
        if not self.folder:
            return
        try:
            now = time.time()
            rows = [{"ts_utc": _iso(now), **s} for s in self.summary()["agents"]]
            if rows:
                with self._csv_lock:
                    self._append(self._csv_path("agent_metrics", now), METRIC_COLUMNS, rows)
        except Exception:
            log.exception("could not write the agent metrics snapshot")

    def csv_files(self) -> list[dict]:
        if not self.folder or not self.folder.exists():
            return []
        out = []
        for p in sorted(self.folder.glob("*.csv"), reverse=True):
            if CSV_NAME.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "bytes": st.st_size, "modified": st.st_mtime})
        return out

    def csv_path(self, name: str) -> Path | None:
        if not self.folder or not CSV_NAME.match(name):
            return None
        p = (self.folder / name).resolve()
        return p if p.parent == self.folder.resolve() and p.is_file() else None

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        for p in self.folder.glob("*.csv"):
            if CSV_NAME.match(p.name) and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    # --- queries for the dashboard ----------------------------------------------------------
    def _window(self, since: float = 0, agent: str = "") -> list[AgentRun]:
        with self._lock:
            runs = list(self.runs)
        return [r for r in runs if r.started >= since and (not agent or r.agent == agent)]

    def query(self, agent: str = "", status: str = "", q: str = "", limit: int = 100, since: float = 0) -> list[dict]:
        q = q.strip().lower()
        out = []
        for r in reversed(self._window(since, agent)):
            if status and r.status != status:
                continue
            if q and not any(q in v for v in (r.run_id, r.correlation_id.lower(), r.trace_id, r.parent_run_id,
                                                r.input_summary.lower(), r.error.lower())):
                continue
            out.append(r.as_dict(self.prices))
            if len(out) >= limit:
                break
        return out

    def run_by_id(self, run_id: str) -> dict | None:
        with self._lock:
            r = next((r for r in self.runs if r.run_id == run_id), None) or self.active.get(run_id)
        return r.as_dict(self.prices) if r else None

    def workflow(self, correlation_id: str) -> list[dict]:
        with self._lock:
            runs = [r for r in self.runs if r.correlation_id == correlation_id]
        runs += [r for r in list(self.active.values()) if r.correlation_id == correlation_id]
        # Parents first on a tie: the turn and its Planner can start in the same millisecond.
        return [r.as_dict(self.prices) for r in sorted(runs, key=lambda r: (round(r.started, 3), bool(r.parent_run_id)))]

    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        v = sorted(values)
        return round(v[min(len(v) - 1, int(p / 100 * len(v)))], 1)

    def summary(self, since: float = 0, agent: str = "") -> dict:
        runs = self._window(since, agent)
        now = time.time()
        by: dict[str, list[AgentRun]] = {}
        for r in runs:
            by.setdefault(r.agent, []).append(r)
        agents = []
        for name, rs in sorted(by.items()):
            lat = [r.latency_ms for r in rs]
            ok = sum(1 for r in rs if r.status == "ok")
            err = sum(1 for r in rs if r.status == "error")
            recent = [r for r in rs if r.started >= now - 300]
            recent_err = sum(1 for r in recent if r.status == "error")
            last = max(rs, key=lambda r: r.started)
            health = ("failing" if recent and recent_err / len(recent) >= 0.5 else
                      "degraded" if recent_err else "idle" if not recent else "healthy")
            agents.append({
                "agent": name, "runs": len(rs), "ok": ok, "errors": err, "other_status": len(rs) - ok - err,
                "success_rate": round(100 * ok / len(rs), 1), "latency_p50_ms": round(statistics.median(lat), 1),
                "latency_p95_ms": self._pct(lat, 95), "latency_max_ms": max(lat),
                "tokens_in": sum(r.tokens_in for r in rs), "tokens_out": sum(r.tokens_out for r in rs),
                "cost_usd": round(sum(r.cost(self.prices) for r in rs), 6), "llm_calls": sum(r.llm_calls for r in rs),
                "tool_calls": sum(len(r.tool_calls) for r in rs), "tool_errors": sum(1 for r in rs for t in r.tool_calls if not t["ok"]),
                "retries": sum(r.retries for r in rs), "last_run": last.started, "last_status": last.status,
                "runs_last_5m": len(recent), "errors_last_5m": recent_err, "health": health,
                "active": sum(1 for a in list(self.active.values()) if a.agent == name)})
        handoffs: dict[tuple[str, str], int] = {}
        for r in runs:
            for src in filter(None, (x.strip() for x in r.handoff_from.split(","))):
                handoffs[(src, r.agent)] = handoffs.get((src, r.agent), 0) + 1
        turns = [r for r in runs if not r.parent_run_id]
        turn_lat = [r.latency_ms for r in turns]
        ok_all = sum(1 for r in runs if r.status == "ok")
        totals = {
            "runs": len(runs), "turns": len(turns), "ok": ok_all, "errors": sum(1 for r in runs if r.status == "error"),
            "success_rate": round(100 * ok_all / len(runs), 1) if runs else None,
            "turn_latency_p50_ms": self._pct(turn_lat, 50), "turn_latency_p95_ms": self._pct(turn_lat, 95),
            "tokens_in": sum(r.tokens_in for r in runs), "tokens_out": sum(r.tokens_out for r in runs),
            "cost_usd": round(sum(r.cost(self.prices) for r in runs), 6), "llm_calls": sum(r.llm_calls for r in runs),
            "tool_calls": sum(len(r.tool_calls) for r in runs), "tool_errors": sum(1 for r in runs for t in r.tool_calls if not t["ok"]),
            "retries": sum(r.retries for r in runs), "handoffs": sum(handoffs.values()), "active": len(self.active)}
        return {"since": since or self.started, "app_started": self.started, "now": now, "runs_in_buffer": len(self.runs),
                "history_loaded": self.history_loaded, "prices_per_mtok": {"in": self.prices[0], "out": self.prices[1]},
                "totals": totals, "agents": agents,
                "handoffs": [{"from": a, "to": b, "handoff": f"{a} → {b}", "count": n}
                             for (a, b), n in sorted(handoffs.items(), key=lambda kv: -kv[1])]}

    def timeseries(self, since: float, bucket_s: int, agent: str = "") -> dict:
        now = time.time()
        start = since - since % bucket_s
        n = max(1, int((now - start) // bucket_s) + 1)
        buckets = [{"t": start + i * bucket_s, "ok": 0, "error": 0, "other": 0, "lat": [], "tokens_in": 0, "tokens_out": 0,
                    "cost_usd": 0.0, "tool_calls": 0, "tool_errors": 0, "retries": 0, "llm_calls": 0} for i in range(n)]
        for r in self._window(since, agent):
            b = buckets[min(n - 1, int((r.started - start) // bucket_s))]
            b["ok" if r.status == "ok" else "error" if r.status == "error" else "other"] += 1
            if agent or not r.parent_run_id:  # latency of whole turns, or of the chosen agent
                b["lat"].append(r.latency_ms)
            b["tokens_in"] += r.tokens_in
            b["tokens_out"] += r.tokens_out
            b["cost_usd"] += r.cost(self.prices)
            b["tool_calls"] += len(r.tool_calls)
            b["tool_errors"] += sum(1 for t in r.tool_calls if not t["ok"])
            b["retries"] += r.retries
            b["llm_calls"] += r.llm_calls
        for b in buckets:
            lat = b.pop("lat")
            b["latency_p50_ms"], b["latency_p95_ms"] = self._pct(lat, 50), self._pct(lat, 95)
            b["cost_usd"] = round(b["cost_usd"], 6)
        return {"bucket_s": bucket_s, "since": since, "points": buckets}

    def tools(self, since: float = 0, agent: str = "") -> list[dict]:
        stats: dict[str, dict] = {}
        for r in self._window(since, agent):
            for t in r.tool_calls:
                s = stats.setdefault(t["tool"], {"tool": t["tool"], "calls": 0, "errors": 0, "ms": [], "agents": set()})
                s["calls"] += 1
                s["errors"] += 0 if t["ok"] else 1
                s["ms"].append(t["ms"])
                s["agents"].add(r.agent)
        out = []
        for s in stats.values():
            ms = s.pop("ms")
            out.append({**s, "agents": sorted(s["agents"]), "error_rate": round(100 * s["errors"] / s["calls"], 1),
                        "avg_ms": round(sum(ms) / len(ms), 1), "p95_ms": self._pct(ms, 95)})
        return sorted(out, key=lambda s: -s["calls"])

    def error_groups(self, since: float = 0, agent: str = "") -> list[dict]:
        """Errors grouped by agent and message (run errors and failed tool calls), newest first."""
        groups: dict[tuple[str, str, str], dict] = {}
        for r in self._window(since, agent):
            items = ([("run", r.error)] if r.status == "error" else []) + [(f"tool {t['tool']}", t["error"] or "failed")
                                                                          for t in r.tool_calls if not t["ok"]]
            for kind, msg in items:
                key = (r.agent, kind, re.sub(r"\d+", "#", msg)[:160])
                g = groups.setdefault(key, {"agent": r.agent, "kind": kind, "message": msg, "count": 0, "first": r.started,
                                            "last": r.started, "last_run_id": r.run_id, "last_correlation_id": r.correlation_id})
                g["count"] += 1
                if r.started >= g["last"]:
                    g.update(last=r.started, last_run_id=r.run_id, last_correlation_id=r.correlation_id, message=msg)
        return sorted(groups.values(), key=lambda g: -g["last"])


observer = AgentObserver()
