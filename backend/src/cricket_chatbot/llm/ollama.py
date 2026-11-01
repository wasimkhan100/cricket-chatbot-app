"""Async Ollama client with timeouts and circuit breakers.

Chat and embeddings have separate breakers, so a broken embedding model only disables knowledge search,
not the whole chatbot.
"""

import json
import logging
import time
from typing import AsyncIterator

import httpx

from ..observability.metrics import LLM_CALLS, LLM_TOKENS
from ..observability.tracing import tracer
from ..resilience.circuit_breaker import CircuitBreaker, CircuitOpenError
from ..resilience.health import HealthRegistry

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    pass


class ModelNotFound(LLMUnavailable):
    """The model is not pulled. A config problem, not an outage, so it does not trip the breaker."""


class OllamaClient:
    def __init__(self, base_url: str, health: HealthRegistry, *, timeout_s: float, connect_timeout_s: float,
                 embed_timeout_s: float, breaker_threshold: int, breaker_reset_s: float):
        self.base_url = base_url.rstrip("/")
        self.health = health
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s))
        self._embed_timeout = httpx.Timeout(embed_timeout_s, connect=connect_timeout_s)
        self.chat_breaker: CircuitBreaker = health.breaker("ollama_chat", failure_threshold=breaker_threshold, reset_after_s=breaker_reset_s)
        self.embed_breaker: CircuitBreaker = health.breaker("ollama_embed", failure_threshold=breaker_threshold, reset_after_s=breaker_reset_s)

    async def close(self) -> None:
        await self._http.aclose()

    async def list_models(self) -> list[str]:
        r = await self._http.get("/api/tags", timeout=3.0)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        async def go():
            self.health.check_chaos("ollama_embed")
            with tracer().start_as_current_span("ollama.embed") as span:
                span.set_attribute("llm.model", model)
                span.set_attribute("embed.count", len(texts))
                r = await self._http.post("/api/embed", json={"model": model, "input": texts, "keep_alive": "30m"}, timeout=self._embed_timeout)
                r.raise_for_status()
                return r.json()["embeddings"]

        try:
            out = await self.embed_breaker.call(go)
            LLM_CALLS.labels(model, "embed", "ok").inc()
            return out
        except CircuitOpenError:
            LLM_CALLS.labels(model, "embed", "circuit_open").inc()
            raise LLMUnavailable("embedding model unavailable") from None
        except Exception as e:
            LLM_CALLS.labels(model, "embed", "error").inc()
            raise LLMUnavailable(f"embedding failed: {e}") from e

    async def chat_stream(self, *, model: str, messages: list[dict], tools: list[dict] | None = None,
                          think: bool = False, options: dict | None = None,
                          keep_alive: str = "30m") -> AsyncIterator[dict]:
        """Yields Ollama stream chunks. Breaker accounting: failure if the call errors, success when it completes."""
        breaker = self.chat_breaker
        try:
            self.health.check_chaos("ollama_chat")
        except Exception as e:
            breaker.record_failure(e)
            LLM_CALLS.labels(model, "chat", "error").inc()
            raise LLMUnavailable(str(e)) from e
        if not breaker.allow():
            LLM_CALLS.labels(model, "chat", "circuit_open").inc()
            raise LLMUnavailable("chat model unavailable (circuit open)")

        body: dict = {"model": model, "messages": messages, "stream": True, "options": options or {}, "keep_alive": keep_alive}
        if tools:
            body["tools"] = tools
        if think:
            body["think"] = True

        t0 = time.monotonic()
        with tracer().start_as_current_span("ollama.chat") as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.think", think)
            span.set_attribute("llm.tools", len(tools or []))
            first = True
            try:
                async with self._http.stream("POST", "/api/chat", json=body) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode()[:300]
                        if r.status_code == 404:
                            raise ModelNotFound(f"model {model} not found in Ollama; run: ollama pull {model}")
                        raise httpx.HTTPStatusError(f"ollama {r.status_code}: {detail}", request=r.request, response=r)
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if "error" in chunk:
                            raise RuntimeError(chunk["error"])
                        if first:
                            span.set_attribute("llm.first_chunk_ms", round((time.monotonic() - t0) * 1000))
                            first = False
                        if chunk.get("done"):
                            pin, pout = chunk.get("prompt_eval_count", 0), chunk.get("eval_count", 0)
                            LLM_TOKENS.labels(model, "in").inc(pin)
                            LLM_TOKENS.labels(model, "out").inc(pout)
                            span.set_attribute("llm.tokens_in", pin)
                            span.set_attribute("llm.tokens_out", pout)
                        yield chunk
            except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as e:
                breaker.record_failure(e)
                LLM_CALLS.labels(model, "chat", "error").inc()
                span.record_exception(e)
                log.warning("ollama chat failed", extra={"model": model, "error": str(e)[:200]})
                raise LLMUnavailable(f"chat failed: {e}") from e
        breaker.record_success()
        LLM_CALLS.labels(model, "chat", "ok").inc()
