"""HTTP API."""

import asyncio
import json
import statistics
import time
from collections import Counter, deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from ..observability.agent_runs import observer
from ..observability.log_tail import tail_logs
from ..observability.logging import request_id_var
from ..observability.metrics import RATE_LIMITED, SHED
from ..observability.transcripts import session_key
from ..observability.tracing import recent_errors, recent_traces
from ..resilience.health import CHAOS_TARGETS, LEVEL_NAMES
from ..security.auth import admin_dependency, client_ip, issue_token, session_dependency

router = APIRouter()
TURN_STATUS = {"degraded": "degraded", "shed": "degraded", "blocked": "blocked"}  # answer path -> turn status
MAX_TICKER_CLIENTS = 500
answer_log: deque[dict] = deque(maxlen=300)


def sse(data: dict) -> bytes:
    return f"data: {json.dumps(data, default=str)}\n\n".encode()


def _limited(scope: str, retry: float) -> JSONResponse:
    RATE_LIMITED.labels(scope).inc()
    return JSONResponse({"detail": f"rate limit exceeded ({scope}); slow down", "scope": scope, "retry_after": round(retry, 1)},
                        status_code=429, headers={"Retry-After": str(max(1, round(retry)))})


# --- sessions ----------------------------------------------------------------------
@router.post("/api/session")
async def create_session(request: Request):
    st = request.app.state
    ok, retry = st.ip_session_limiter.acquire(client_ip(request))
    if not ok:
        return _limited("session_per_ip", retry)
    sid, token = issue_token(st.settings.session_secret)
    return {"token": token, "session_id": sid, "expires_in": st.settings.session_ttl_s}


# --- chat ----------------------------------------------------------------------------
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    think: bool = False


@router.post("/api/chat")
async def chat(body: ChatRequest, request: Request, sid: str = Depends(session_dependency)):
    st = request.app.state
    for scope, limiter, key in (("global", st.global_limiter, "all"), ("ip", st.ip_limiter, client_ip(request)),
                                ("session", st.session_limiter, sid)):
        ok, retry = limiter.acquire(key)
        if not ok:
            return _limited(scope, retry)

    rid = request_id_var.get()  # the correlation id that ties every agent run of this turn together

    async def stream():
        request_id_var.set(rid)  # the body streams after the middleware returns: keep the id for the whole turn
        events: list[dict] = []  # everything that happened this turn, for the session transcript
        with observer.run("Crickbot", body.question, session=session_key(sid)) as turn:
            events.append({"type": "ids", "correlation_id": rid, "trace_id": turn.trace_id, "run_id": turn.run_id})
            try:
                async for event in st.agent.answer(body.question, sid, think=body.think):
                    events.append(event)
                    if event["type"] == "done":
                        event = {k: v for k, v in event.items() if k != "evidence"}  # internal: transcripts and evals only
                        event["correlation_id"] = rid
                        answer_log.append({"ts": time.time(), "path": event["path"], "latency_ms": event["latency_ms"],
                                           "first_token_ms": event["first_token_ms"], "tokens": event["tokens"]})
                        turn.set_output(f"[{event['path']}] {event['answer']}")
                        turn.status = TURN_STATUS.get(event["path"], "ok")
                    yield sse(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never leave the browser hanging
                st.log.exception("chat stream failed")
                turn.status, turn.error = "error", f"{type(e).__name__}: {e}"[:300]
                events.append({"type": "error", "message": "chat stream failed", "detail": type(e).__name__})
                yield sse({"type": "error", "message": "Something went wrong answering that. Please try again.",
                           "detail": type(e).__name__})
            finally:
                if events:  # also on disconnect: a partial turn is still worth keeping
                    try:
                        await asyncio.to_thread(st.transcripts.record_turn, sid, body.question, events)
                    except Exception:
                        st.log.exception("could not write the transcript")

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/api/transcript")
async def my_transcript(request: Request, sid: str = Depends(session_dependency)):
    """The caller's own session transcript: every turn, route, agent and tool call."""
    st = request.app.state
    p = await asyncio.to_thread(st.transcripts.path_for, sid)
    if p is None:
        return {"name": None, "content": "No questions in this session yet."}
    return {"name": p.name, "content": await asyncio.to_thread(p.read_text, errors="replace")}


@router.delete("/api/chat/history")
async def clear_history(request: Request, sid: str = Depends(session_dependency)):
    request.app.state.sessions.clear(sid)
    return {"ok": True}


class Feedback(BaseModel):
    message_id: str = Field(max_length=64)
    rating: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=300)


@router.post("/api/feedback")
async def feedback(body: Feedback, request: Request, sid: str = Depends(session_dependency)):
    request.app.state.log.info("feedback", extra={"session": sid, "message_id": body.message_id, "rating": body.rating})
    return {"ok": True}


# --- live match ----------------------------------------------------------------------
@router.get("/api/live")
async def live(request: Request):
    feed = request.app.state.feed
    etag = f'W/"v{feed.state.version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(feed.state.to_public(), headers={"ETag": etag, "Cache-Control": "public, max-age=1"})


@router.get("/api/live/stream")
async def live_stream(request: Request):
    feed = request.app.state.feed
    if feed.broadcaster.count >= MAX_TICKER_CLIENTS:
        SHED.labels("ticker_full").inc()
        raise HTTPException(503, "too many live viewers; poll GET /api/live instead", headers={"Retry-After": "5"})
    q = feed.broadcaster.subscribe()

    async def stream():
        try:
            yield sse({"type": "state", "state": feed.state.to_public()})
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield sse({"type": "state", "state": item})
                except TimeoutError:
                    yield b": keep-alive\n\n"
        finally:
            feed.broadcaster.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/api/suggestions")
async def suggestions(request: Request):
    from ..agent.router import suggestions as sugg

    return {"suggestions": sugg(request.app.state.feed.state)}


# --- health and observability ---------------------------------------------------------
@router.get("/health/live")
async def health_live():
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(request: Request):
    snap = request.app.state.health.snapshot()
    # Ready while the app can still answer something: the ticker and templates need only the feed state.
    status = 200 if request.app.state.feed is not None else 503
    return JSONResponse({"status": "ready" if status == 200 else "not_ready", **snap}, status_code=status)


@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _summary() -> dict:
    window = [a for a in answer_log if time.time() - a["ts"] < 600]
    lat = sorted(a["latency_ms"] for a in window)
    llm = sorted(a["first_token_ms"] for a in window if a["path"] == "llm" and a["first_token_ms"])

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None

    return {
        "window_s": 600, "answers": len(window), "by_path": dict(Counter(a["path"] for a in window)),
        "latency_p50_ms": round(statistics.median(lat)) if lat else None, "latency_p95_ms": pct(lat, 0.95),
        "llm_first_token_p50_ms": round(statistics.median(llm)) if llm else None,
        "tokens_out": sum(a["tokens"] or 0 for a in window),
    }


@router.get("/api/ops/status")
async def ops_status(request: Request):
    st = request.app.state
    snap = st.health.snapshot()
    for c in snap["components"]:
        c.pop("last_error", None)  # error text stays behind the admin token
    return {
        **snap,
        "feed": st.feed.progress | {"version": st.feed.state.version, "status": st.feed.state.status},
        "cache": st.cache.snapshot(),
        "sessions": len(st.sessions),
        "ticker_clients": st.feed.broadcaster.count,
        "models": {"chat": st.settings.chat_model, "think": st.settings.think_model, "embed": st.settings.embed_model,
                   "available": st.models_available},
        "knowledge_docs": st.knowledge_count,
        "answers": _summary(),
        "limits": {"per_client_per_min": st.limits["rate_per_client_per_min"], "burst": st.limits["rate_burst_per_client"],
                   "global_per_s": st.limits["rate_global_per_s"]},
        "uptime_s": round(time.time() - st.started_at),
    }


@router.get("/api/ops/timeseries")
async def ops_timeseries(request: Request, minutes: int = Query(default=10, ge=1, le=15)):
    m = request.app.state.monitor
    cutoff = time.time() - max(1, min(minutes, 15)) * 60
    return {"interval_s": m.interval_s, "points": [p for p in m.points if p["ts"] >= cutoff]}


@router.get("/api/ops/alerts")
async def ops_alerts(request: Request):
    return request.app.state.monitor.alerts()


# --- admin (X-Admin-Token) -------------------------------------------------------------
admin = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_dependency)])


@admin.get("/traces")
async def traces(limit: int = Query(default=15, ge=1, le=50)):
    return {"traces": recent_traces.recent(min(limit, 50))}


@admin.get("/transcripts")
async def transcripts(request: Request):
    return {"folder": "backend/var/conversations", "files": await asyncio.to_thread(request.app.state.transcripts.files)}


@admin.get("/transcripts/{name}")
async def transcript(name: str, request: Request):
    text = await asyncio.to_thread(request.app.state.transcripts.read, name)
    if text is None:
        raise HTTPException(404, "no such transcript")
    return {"name": name, "content": text}


# --- agent observability ---------------------------------------------------------------
RANGE_S = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400}
BUCKET_S = {"15m": 30, "1h": 120, "6h": 600, "24h": 1800}


def _since(range_: str) -> float:
    return time.time() - RANGE_S.get(range_, 3600)


@admin.get("/agents/summary")
async def agents_summary(range: str = Query(default="1h", pattern="^(15m|1h|6h|24h)$"), agent: str = Query(default="", max_length=40)):
    return observer.summary(_since(range), agent)


@admin.get("/agents/timeseries")
async def agents_timeseries(range: str = Query(default="1h", pattern="^(15m|1h|6h|24h)$"), agent: str = Query(default="", max_length=40)):
    return observer.timeseries(_since(range), BUCKET_S[range], agent)


@admin.get("/agents/tools")
async def agents_tools(range: str = Query(default="1h", pattern="^(15m|1h|6h|24h)$"), agent: str = Query(default="", max_length=40)):
    return {"tools": observer.tools(_since(range), agent)}


@admin.get("/agents/errors")
async def agents_errors(range: str = Query(default="1h", pattern="^(15m|1h|6h|24h)$"), agent: str = Query(default="", max_length=40)):
    return {"errors": observer.error_groups(_since(range), agent)}


@admin.get("/agents/runs")
async def agents_runs(agent: str = Query(default="", max_length=40), status: str = Query(default="", max_length=20),
                      q: str = Query(default="", max_length=64), limit: int = Query(default=100, ge=1, le=500),
                      range: str = Query(default="24h", pattern="^(15m|1h|6h|24h)$")):
    """Filter by agent, status, or a run id / correlation id / trace id / question text."""
    return {"runs": observer.query(agent, status, q, limit, _since(range))}


@admin.get("/agents/runs/{run_id}")
async def agents_run(run_id: str):
    run = observer.run_by_id(run_id[:32])
    if run is None:
        raise HTTPException(404, "no such run (the buffer keeps the latest 3000 runs and the last 24 h from CSV)")
    return run


@admin.get("/agents/logs")
async def agents_logs(request: Request, q: str = Query(default="", max_length=100), level: str = Query(default="", max_length=10),
                      logger: str = Query(default="", max_length=40), agent: str = Query(default="", max_length=40),
                      correlation_id: str = Query(default="", max_length=64), run_id: str = Query(default="", max_length=32),
                      limit: int = Query(default=200, ge=1, le=1000), hide_polling: bool = True):
    path = request.app.state.settings.var_dir / "app.log"
    return await asyncio.to_thread(tail_logs, path, q=q, level=level, logger=logger, agent=agent,
                                   correlation_id=correlation_id, run_id=run_id, limit=limit, hide_polling=hide_polling)


@admin.get("/agents/workflow/{correlation_id}")
async def agents_workflow(correlation_id: str):
    runs = observer.workflow(correlation_id[:64])
    if not runs:
        raise HTTPException(404, "no agent runs for that correlation id (the buffer keeps the latest 3000 runs)")
    trace_ids = {r["trace_id"] for r in runs if r["trace_id"]}
    traces = [t for t in recent_traces.recent(50) if t["trace_id"] in trace_ids]
    return {"correlation_id": correlation_id, "runs": runs, "traces": traces}


@admin.get("/agents/csv")
async def agents_csv_files():
    return {"folder": "backend/var/metrics", "files": await asyncio.to_thread(observer.csv_files)}


@admin.get("/agents/csv/{name}")
async def agents_csv_file(name: str):
    p = observer.csv_path(name)
    if p is None:
        raise HTTPException(404, "no such CSV file")
    data = await asyncio.to_thread(p.read_bytes)
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{p.name}"'})


@admin.get("/errors")
async def errors(request: Request):
    return {"errors": recent_errors(), "components": request.app.state.health.snapshot()["components"]}


class ChaosBody(BaseModel):
    target: str
    enabled: bool


@admin.post("/chaos")
async def chaos(body: ChaosBody, request: Request):
    if body.target not in CHAOS_TARGETS:
        raise HTTPException(422, f"target must be one of {CHAOS_TARGETS}")
    h = request.app.state.health
    (h.chaos.add if body.enabled else h.chaos.discard)(body.target)
    request.app.state.log.warning("chaos switched", extra={"target": body.target, "enabled": body.enabled})
    return {"chaos": sorted(h.chaos)}


class DegradeBody(BaseModel):
    level: int | None = Field(default=None, ge=0, le=2)


@admin.post("/degrade")
async def degrade(body: DegradeBody, request: Request):
    request.app.state.health.forced_level = body.level
    return {"forced_level": body.level, "names": LEVEL_NAMES}


class FeedBody(BaseModel):
    model_config = {"allow_inf_nan": False}
    action: str = Field(pattern="^(pause|resume|restart|speed|step)$")
    value: float | None = Field(default=None, ge=-1, le=1_000_000)


@admin.post("/feed")
async def feed_control(body: FeedBody, request: Request):
    feed = request.app.state.feed
    if body.action == "pause":
        feed.pause(True)
    elif body.action == "resume":
        feed.pause(False)
    elif body.action == "restart":
        try:
            await feed.restart(int(body.value) if body.value else None)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        request.app.state.cache.clear()
    elif body.action == "speed" and body.value:
        feed.set_speed(body.value)
    elif body.action == "step":
        # Bounded: a huge value must not spin the event loop. A T20 match is about 250 deliveries.
        for _ in range(max(1, min(int(body.value or 1), 300))):
            if not feed.apply_next():
                break
    return feed.progress


@admin.post("/cache/clear")
async def cache_clear(request: Request):
    request.app.state.cache.clear()
    return {"ok": True}


class LimitsBody(BaseModel):
    model_config = {"allow_inf_nan": False}  # NaN/Infinity would poison the token buckets
    rate_per_client_per_min: int | None = Field(default=None, ge=1, le=10_000)
    rate_burst_per_client: int | None = Field(default=None, ge=1, le=1_000)
    rate_global_per_s: float | None = Field(default=None, gt=0, le=10_000)
    llm_queue_max: int | None = Field(default=None, ge=0, le=1_000)
    llm_queue_timeout_s: float | None = Field(default=None, gt=0, le=600)


@admin.get("/limits")
async def get_limits(request: Request):
    from .selftest import current_limits, default_limits

    st = request.app.state
    return {"current": current_limits(st), "defaults": default_limits(st.settings)}


@admin.post("/limits")
async def set_limits(body: LimitsBody, request: Request):
    from .selftest import apply_limits

    st = request.app.state
    new = apply_limits(st, **body.model_dump())
    st.log.warning("limits changed", extra={"limits": new})
    return {"current": new}


@admin.post("/limits/reset")
async def reset_limits(request: Request):
    from .selftest import apply_limits, default_limits

    st = request.app.state
    return {"current": apply_limits(st, **default_limits(st.settings))}


_selftest_lock = asyncio.Lock()


@admin.post("/selftest/{name}")
async def selftest(name: str, request: Request):
    from .selftest import run_test

    if name not in ("rate_limit", "throttle", "load_shed"):
        raise HTTPException(404, "test must be rate_limit, throttle or load_shed")
    if _selftest_lock.locked():
        raise HTTPException(409, "another test is running")

    async def stream():
        async with _selftest_lock:
            request.app.state.log.warning("self-test started", extra={"test": name})
            async for event in run_test(request.app, name):
                yield sse(event)
            request.app.state.log.warning("self-test finished", extra={"test": name})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
