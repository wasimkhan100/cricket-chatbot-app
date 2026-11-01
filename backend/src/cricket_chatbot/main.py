"""Application wiring. Run:  uv run python -m cricket_chatbot.main"""

import asyncio
import contextlib
import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agent.agent import Agent, AgentDeps
from .agents.orchestrator import COMMENTATOR_PROMPT
from .agent.memory import SessionMemory, SingleFlight, TTLCache
from .api.routes import admin, router
from .api.selftest import apply_limits, default_limits
from .observability.history import Monitor
from .config import DEV_ADMIN_TOKEN, DEV_SESSION_SECRET, Settings, get_settings
from .ingest import ensure_database
from .live.feed import LiveFeed
from .llm.ollama import LLMUnavailable, OllamaClient
from .observability.logging import setup_logging
from .observability.tracing import setup_tracing
from .observability.agent_runs import observer
from .observability.logging import register_secret
from .observability.transcripts import TranscriptStore
from .rag.store import KnowledgeStore
from .resilience.health import HealthRegistry
from .resilience.load_shed import AdmissionController
from .resilience.rate_limit import TokenBucketLimiter
from .security.middleware import SecurityMiddleware

log = logging.getLogger("cricket_chatbot")


async def _probe_loop(app: FastAPI) -> None:
    """Background health probes. Also what closes a breaker again once a dependency recovers,
    since at degrade level 2 no fan traffic reaches the LLM to probe it."""
    st = app.state
    while True:
        try:
            models = await st.llm.list_models()
            st.models_available = models
            notes = st.health.component_notes
            for key, model in (("ollama_chat", st.settings.chat_model), ("ollama_embed", st.settings.embed_model)):
                present = any(m == model or m.split(":")[0] == model or m == f"{model}:latest" for m in models)
                notes[key] = model if present else f"{model} not pulled"
                b = st.health.breakers[key]
                if present and b.state.value != "closed" and key not in st.health.chaos:
                    b.record_success()
            think_ok = any(m == st.settings.think_model for m in models)
            notes["ollama_chat"] += "" if think_ok else f" · thinking model {st.settings.think_model} not pulled"
        except Exception as e:
            st.models_available = []
            st.health.breakers["ollama_chat"].record_failure(e)
            st.health.breakers["ollama_embed"].record_failure(e)
        for name, check in (("stats_db", st.stats.ping), ("chroma", st.knowledge.count)):
            b = st.health.breaker(name)
            if name in st.health.chaos:
                continue
            try:
                result = await asyncio.to_thread(check)
                if name == "chroma":
                    st.knowledge_count = result
                    st.health.component_notes["chroma"] = f"{result} documents"
                if b.state.value != "closed":
                    b.record_success()
            except Exception as e:
                b.record_failure(e)
        await asyncio.sleep(5)


async def _agent_metrics_snapshots(every_s: float = 60.0) -> None:
    """Append a per-agent metrics row to var/metrics/agent_metrics_<date>.csv every minute."""
    while True:
        await asyncio.sleep(every_s)
        await asyncio.to_thread(observer.write_metrics_snapshot)


async def _warm_up(app: FastAPI) -> None:
    """Load the chat model into memory so the first fan doesn't wait for it."""
    st = app.state
    try:
        # The Commentator agent's exact system prompt and (empty) tool list, so Ollama's prefix cache is primed too.
        async for _ in st.llm.chat_stream(model=st.settings.chat_model, tools=None,
                                          messages=[{"role": "system", "content": COMMENTATOR_PROMPT}, {"role": "user", "content": "hi"}],
                                          options={"num_predict": 1, "num_ctx": st.settings.llm_num_ctx}):
            pass
        log.info("chat model warmed up", extra={"model": st.settings.chat_model})
    except LLMUnavailable as e:
        log.warning("warm-up failed; chat will run degraded until Ollama is reachable", extra={"error": str(e)[:200]})


async def _ai_commentary(app: FastAPI) -> None:
    """Adds an AI line after wickets and sixes, only when no fan is waiting for the LLM (spare capacity only)."""
    st = app.state
    q = st.feed.broadcaster.subscribe()
    seen = 0
    try:
        while True:
            item = await q.get()
            top = (item.get("commentary") or [{}])[0]
            v = top.get("version", 0)
            big_moment = top.get("kind") == "wicket" or "SIX" in top.get("text", "")
            if v <= seen or not big_moment:
                continue
            seen = v
            if st.health.level != 0 or st.admission.inflight or st.admission.waiting:
                continue
            prompt = (f"Write one vivid line of TV commentary (max 25 words, no numbers that aren't given) for this moment: "
                      f"{top['text']} Match: {item['title']}. Last event: {item['last_event']}.")
            text = ""
            try:
                async with st.admission.slot():
                    # Same prefix as the Commentator agent, so idle commentary doesn't evict its prompt cache.
                    async for chunk in st.llm.chat_stream(model=st.settings.chat_model, tools=None,
                                                          messages=[{"role": "system", "content": COMMENTATOR_PROMPT},
                                                                    {"role": "user", "content": prompt}],
                                                          options={"num_predict": 60, "temperature": 0.8,
                                                                   "num_ctx": st.settings.llm_num_ctx}):
                        text += (chunk.get("message") or {}).get("content", "")
            except Exception:
                continue
            text = text.strip().strip('"')
            if text:
                st.feed.builder.add_commentary(v, top.get("over", ""), text, kind="ai")
    except asyncio.CancelledError:
        st.feed.broadcaster.unsubscribe(q)
        raise


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Fail closed: the default secrets are published with the code, so running on them would hand anyone admin access.
    if not settings.allow_dev_secrets and (settings.admin_token == DEV_ADMIN_TOKEN or settings.session_secret == DEV_SESSION_SECRET):
        raise RuntimeError("Refusing to start with the default admin token or session secret. Run scripts/setup.sh "
                           "(it writes fresh secrets to backend/.env), or set CRICKET_ALLOW_DEV_SECRETS=true for a throwaway test.")
    setup_logging(settings.log_level, settings.var_dir / "app.log")
    setup_tracing("cricket-chatbot", settings.otlp_endpoint)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        st = app.state
        st.settings = settings
        st.log = log
        st.started_at = time.time()
        st.models_available = []
        st.knowledge_count = 0
        st.health = HealthRegistry()
        st.admission = AdmissionController(settings.llm_max_concurrency, settings.llm_queue_max, settings.llm_queue_timeout_s)
        st.health.admission = st.admission
        bk = {"failure_threshold": settings.breaker_failure_threshold, "reset_after_s": settings.breaker_reset_s}
        st.health.breaker("stats_db", **bk)
        st.stats = ensure_database(settings=settings)
        st.llm = OllamaClient(settings.ollama_url, st.health, timeout_s=settings.llm_timeout_s,
                              connect_timeout_s=settings.llm_connect_timeout_s, embed_timeout_s=settings.embed_timeout_s,
                              breaker_threshold=settings.breaker_failure_threshold, breaker_reset_s=settings.breaker_reset_s)
        st.knowledge = KnowledgeStore(settings.chroma_path, settings.chroma_collection, st.llm, settings.embed_model, st.health,
                                      settings.breaker_failure_threshold, settings.breaker_reset_s)
        st.knowledge_count = st.knowledge.count()
        if st.knowledge_count == 0:
            log.warning("vector index is empty; run: uv run python -m cricket_chatbot.ingest")
        st.feed = LiveFeed(st.stats, settings.replay_match_id, settings.replay_ball_interval_s, st.health)
        st.cache = TTLCache(settings.answer_cache_size, settings.answer_cache_ttl_s)
        st.sessions = SessionMemory()
        st.transcripts = TranscriptStore(settings.var_dir / "conversations")
        observer.configure(settings.var_dir / "metrics",       # agent_runs / tool_calls / agent_metrics CSVs
                           (settings.llm_price_in_per_mtok, settings.llm_price_out_per_mtok))
        for secret in (settings.admin_token, settings.session_secret):
            register_secret(secret)                            # never in logs, CSVs or the dashboard
        st.agent = Agent(AgentDeps(settings=settings, llm=st.llm, stats=st.stats, knowledge=st.knowledge, feed=st.feed,
                                   health=st.health, admission=st.admission, cache=st.cache, flights=SingleFlight(),
                                   sessions=st.sessions))
        st.limits = default_limits(settings)
        apply_limits(st)  # builds the per-fan, per-IP and global token buckets
        st.ip_session_limiter = TokenBucketLimiter(settings.session_create_per_min / 60, settings.session_create_per_min)

        st.monitor = Monitor()
        tasks = [asyncio.create_task(_probe_loop(app), name="probe"),
                 asyncio.create_task(st.monitor.run(app), name="monitor"),
                 asyncio.create_task(_warm_up(app), name="warmup"),
                 asyncio.create_task(_ai_commentary(app), name="ai-commentary"),
                 asyncio.create_task(_agent_metrics_snapshots(), name="agent-metrics")]
        if settings.replay_autostart:
            st.feed.start()
        log.info("cricket chatbot ready", extra={"host": settings.host, "port": settings.port,
                                                  "match_id": settings.replay_match_id, "docs": st.knowledge_count})
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await st.feed.stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            await st.llm.close()

    app = FastAPI(title="Cricket Commentary Chatbot", version="1.0.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Don't echo the rejected input back: it can contain NaN (not valid JSON, which crashed the default
        # handler with a 500) and reflecting user input is an injection risk anyway.
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Admin-Token", "X-Request-ID"],
                       expose_headers=["X-Request-ID", "Retry-After"], max_age=600)
    app.add_middleware(SecurityMiddleware, max_body_bytes=settings.max_body_bytes)
    app.include_router(router)
    app.include_router(admin)

    if settings.frontend_dist.exists():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="ui")
    else:
        log.warning("frontend not built; run: cd frontend && npm run build", extra={"path": str(settings.frontend_dist)})
    return app


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("cricket_chatbot.main:create_app", factory=True, host=s.host, port=s.port, log_config=None,
                proxy_headers=False, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
