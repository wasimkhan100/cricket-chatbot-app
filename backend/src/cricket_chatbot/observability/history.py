"""Metrics history and alerting for the dashboard.

Every few seconds the sampler turns Prometheus counters into per-minute rates, records gauges, and evaluates
alert rules. The dashboard reads the last 10 minutes; alerts are also written to the JSON log, so a log
shipper can page on them.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from prometheus_client import REGISTRY

log = logging.getLogger("alerts")

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _counter_totals() -> dict[str, float]:
    """Sums of the counters the dashboard charts, keyed by a short name."""
    out = {"answers": 0.0, "llm": 0.0, "shed": 0.0, "rate_limited": 0.0, "throttled": 0.0, "server_errors": 0.0,
           "blocked": 0.0, "validator": 0.0, "tokens_out": 0.0}
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if not s.name.endswith("_total"):
                continue
            if s.name == "chat_questions_total":
                out["answers"] += s.value
                if s.labels.get("path") == "llm":
                    out["llm"] += s.value
                if s.labels.get("path") == "blocked":
                    out["blocked"] += s.value
            elif s.name == "load_shed_total":
                out["shed"] += s.value
            elif s.name == "rate_limited_total":
                out["throttled" if s.labels.get("scope") == "global" else "rate_limited"] += s.value
            elif s.name == "http_requests_total" and s.labels.get("status", "").startswith("5"):
                out["server_errors"] += s.value
            elif s.name == "validator_mismatch_total":
                out["validator"] += s.value
            elif s.name == "llm_tokens_total" and s.labels.get("direction") == "out":
                out["tokens_out"] += s.value
    return out


@dataclass
class Alert:
    key: str
    severity: str
    title: str
    detail: str
    since: float


class Monitor:
    def __init__(self, interval_s: float = 2.0, keep_s: int = 900):
        self.interval_s = interval_s
        self.points: deque[dict] = deque(maxlen=int(keep_s / interval_s))
        self.active: dict[str, Alert] = {}
        self.history: deque[dict] = deque(maxlen=200)
        self.rate_window_s = 30.0
        self._totals_hist: deque[tuple[float, dict[str, float]]] = deque()
        self._last_cache = (0, 0)

    # --- sampling ---------------------------------------------------------------
    def sample(self, app) -> dict:
        st = app.state
        now = time.time()
        totals = _counter_totals()
        # Rates over a trailing 30 s window: smooth enough to read, quick enough to show a spike.
        self._totals_hist.append((now, totals))
        while len(self._totals_hist) > 1 and now - self._totals_hist[0][0] > self.rate_window_s:
            self._totals_hist.popleft()
        t_old, old = self._totals_hist[0]
        elapsed = max(now - t_old, self.interval_s)
        rates = {k: round(max(0.0, totals[k] - old[k]) * 60 / elapsed, 1) for k in totals}

        hits, misses = st.cache.hits, st.cache.misses
        dh, dm = hits - self._last_cache[0], misses - self._last_cache[1]
        self._last_cache = (hits, misses)

        from ..api.routes import answer_log
        recent = sorted(a["latency_ms"] for a in answer_log if now - a["ts"] < 60)
        p95 = recent[min(len(recent) - 1, int(0.95 * len(recent)))] if recent else None
        p50 = recent[len(recent) // 2] if recent else None

        health = st.health
        last_ball = health.feed_last_ball_at
        point = {
            "ts": round(now, 1),
            "answers_per_min": rates["answers"],
            "llm_per_min": rates["llm"],
            "rate_limited_per_min": rates["rate_limited"],
            "throttled_per_min": rates["throttled"],
            "shed_per_min": rates["shed"],
            "errors_per_min": rates["server_errors"],
            "blocked_per_min": rates["blocked"],
            "tokens_out_per_min": rates["tokens_out"],
            "llm_inflight": st.admission.inflight,
            "llm_queue": st.admission.waiting,
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "cache_hit_pct": round(100 * dh / (dh + dm)) if dh + dm else None,
            "degrade_level": health.level,
            "feed_lag_s": round(now - last_ball, 1) if last_ball and health.feed_running else None,
            "components_down": sum(1 for c in health.components() if not c["up"]),
            "ticker_clients": st.feed.broadcaster.count,
        }
        self.points.append(point)
        self._evaluate(app, point)
        return point

    def window(self, key: str, seconds: float) -> list:
        cutoff = time.time() - seconds
        return [p[key] for p in self.points if p["ts"] >= cutoff and p.get(key) is not None]

    # --- alert rules ------------------------------------------------------------
    def _evaluate(self, app, p: dict) -> None:
        st = app.state
        firing: dict[str, tuple[str, str, str]] = {}
        names = {"ollama_chat": "Chat model", "ollama_embed": "Embedding model", "chroma": "Vector DB",
                 "stats_db": "Stats DB", "feed": "Live feed"}
        for c in st.health.components():
            if not c["up"]:
                why = "failure injected by operator" if c["chaos"] else (c.get("last_error") or f"breaker {c['breaker']}")
                firing[f"down:{c['name']}"] = ("critical", f"{names.get(c['name'], c['name'])} is down", why[:160])
        if p["degrade_level"] == 2:
            firing["level"] = ("critical", "AI answers offline (level 2)", "Fans get templates, cached and record-book answers only.")
        elif p["degrade_level"] == 1:
            firing["level"] = ("warning", "Busy mode (level 1)", "LLM queue over half full: thinking off, shorter answers.")
        if p["feed_lag_s"] is not None and p["feed_lag_s"] > 20:
            firing["feed_lag"] = ("warning", "Live feed delayed", f"No ball for {p['feed_lag_s']:.0f}s; fans see a stale score.")
        if sum(self.window("errors_per_min", 60)):
            firing["errors"] = ("critical", "Server errors", "HTTP 5xx responses in the last minute. Check the app log.")
        if sum(self.window("shed_per_min", 60)):
            firing["shed"] = ("warning", "Load shedding active", "Questions were shed to quick data answers in the last minute.")
        if sum(self.window("throttled_per_min", 60)):
            firing["throttled"] = ("warning", "Global throttle hit", "Total traffic exceeded the global limit in the last minute.")
        if sum(self.window("rate_limited_per_min", 60)):
            firing["rate_limited"] = ("info", "Fans rate limited", "Some fans got 429 for asking too fast (per-fan or per-IP limit).")
        p95s = self.window("latency_p95_ms", 60)
        if p95s and max(p95s) > 30_000:
            firing["latency"] = ("warning", "Slow answers", f"p95 answer time {max(p95s) / 1000:.0f}s in the last minute.")
        if p["llm_queue"] >= st.admission.max_queue:
            firing["queue_full"] = ("warning", "LLM queue full", "New AI questions are being shed immediately.")

        now = time.time()
        for key, (sev, title, detail) in firing.items():
            if key not in self.active:
                self.active[key] = Alert(key, sev, title, detail, now)
                self.history.appendleft({"ts": now, "event": "fired", "key": key, "severity": sev, "title": title, "detail": detail})
                (log.error if sev == "critical" else log.warning if sev == "warning" else log.info)(
                    f"ALERT fired: {title}", extra={"alert": key, "severity": sev, "detail": detail})
            else:
                a = self.active[key]
                a.severity, a.title, a.detail = sev, title, detail
        for key in list(self.active):
            if key not in firing:
                a = self.active.pop(key)
                self.history.appendleft({"ts": now, "event": "resolved", "key": key, "severity": a.severity,
                                         "title": a.title, "detail": f"lasted {now - a.since:.0f}s"})
                log.info(f"ALERT resolved: {a.title}", extra={"alert": key, "duration_s": round(now - a.since)})

    def alerts(self) -> dict:
        active = sorted(self.active.values(), key=lambda a: (SEVERITY_ORDER[a.severity], a.since))
        worst = active[0].severity if active else None
        overall = {"critical": "down" if any(a.key.startswith("down:") or a.key == "errors" for a in active) else "degraded",
                   "warning": "degraded", "info": "healthy", None: "healthy"}[worst]
        return {"overall": overall, "active": [a.__dict__ for a in active], "history": list(self.history)[:60]}

    async def run(self, app) -> None:
        while True:
            try:
                self.sample(app)
            except Exception:
                log.exception("metrics sampler failed")
            await asyncio.sleep(self.interval_s)
