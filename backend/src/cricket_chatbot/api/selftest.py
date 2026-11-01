"""Live traffic controls and built-in traffic tests.

Limits can be changed at runtime (operators tune throttling during an incident without a restart).
The self-tests send real HTTP requests to this server, through the same middleware, limiters and agent
that fans use, and report a pass/fail verdict.
"""

import asyncio
import json
import secrets
import time
from typing import AsyncIterator

import httpx

from ..resilience.rate_limit import TokenBucketLimiter
from ..security.auth import issue_token

LIMIT_KEYS = ("rate_per_client_per_min", "rate_burst_per_client", "rate_global_per_s", "llm_queue_max", "llm_queue_timeout_s")


def current_limits(st) -> dict:
    return dict(st.limits)


def apply_limits(st, **changes) -> dict:
    """Rebuilds the token buckets and resizes the LLM queue. Existing buckets restart full."""
    lim = st.limits
    for k, v in changes.items():
        if k in LIMIT_KEYS and v is not None:
            lim[k] = type(lim[k])(v)
    per_min = lim["rate_per_client_per_min"] / 60
    st.session_limiter = TokenBucketLimiter(per_min, lim["rate_burst_per_client"])
    st.ip_limiter = TokenBucketLimiter(per_min * 3, lim["rate_burst_per_client"] * 3)
    st.global_limiter = TokenBucketLimiter(lim["rate_global_per_s"], max(1.0, lim["rate_global_per_s"] * 2))
    st.admission.max_queue = lim["llm_queue_max"]
    st.admission.queue_timeout_s = lim["llm_queue_timeout_s"]
    return dict(lim)


def default_limits(settings) -> dict:
    return {k: getattr(settings, k) for k in LIMIT_KEYS}


def _ev(kind: str, **kw) -> dict:
    return {"type": kind, **kw}


async def _ask(client: httpx.AsyncClient, token: str, question: str) -> dict:
    t0 = time.monotonic()
    try:
        async with client.stream("POST", "/api/chat", json={"question": question},
                                 headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status_code != 200:
                body = json.loads(await r.aread() or b"{}")
                return {"status": r.status_code, "path": None, "scope": body.get("scope"),
                        "retry_after": r.headers.get("retry-after"), "ms": round((time.monotonic() - t0) * 1000)}
            path = None
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    e = json.loads(line[6:])
                    if e["type"] == "done":
                        path = e["path"]
            return {"status": 200, "path": path, "scope": None, "retry_after": None, "ms": round((time.monotonic() - t0) * 1000)}
    except httpx.HTTPError as e:
        return {"status": 0, "path": None, "scope": type(e).__name__, "retry_after": None, "ms": round((time.monotonic() - t0) * 1000)}


def _token(st) -> str:
    # Test sessions are minted in-process: session creation has its own per-IP limit, which isn't under test here.
    return issue_token(st.settings.session_secret)[1]


async def run_test(app, name: str) -> AsyncIterator[dict]:
    st = app.state
    base = f"http://127.0.0.1:{st.settings.port}"
    async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(180, connect=5)) as client:
        if name == "rate_limit":
            async for e in _rate_limit(st, client):
                yield e
        elif name == "throttle":
            async for e in _throttle(st, client):
                yield e
        elif name == "load_shed":
            async for e in _load_shed(st, client):
                yield e
        else:
            yield _ev("verdict", passed=False, summary=f"unknown test {name}")


async def _rate_limit(st, client) -> AsyncIterator[dict]:
    burst = st.limits["rate_burst_per_client"]
    n = burst + 4
    yield _ev("step", text=f"One fan sends {n} quick questions. The per-fan bucket holds {burst}, "
                           f"refilling at {st.limits['rate_per_client_per_min']}/min, so requests after #{burst} should get 429.")
    token = _token(st)
    rows = []
    for i in range(n):
        r = await _ask(client, token, "what's the score?")
        r["i"] = i + 1
        rows.append(r)
        yield _ev("request", **r)
    ok = sum(1 for r in rows if r["status"] == 200)
    limited = [r for r in rows if r["status"] == 429]
    passed = burst <= ok <= burst + 1 and len(limited) >= n - burst - 1 and all(r["scope"] == "session" for r in limited)
    retry = limited[0]["retry_after"] if limited else "-"
    yield _ev("verdict", passed=passed,
              summary=f"{ok} allowed, {len(limited)} rejected with 429 (scope: session, Retry-After {retry}s). "
                      + ("The per-fan limit works." if passed else "Expected the first requests allowed and the rest rejected."))


async def _throttle(st, client) -> AsyncIterator[dict]:
    saved = current_limits(st)
    n, fans = 12, 4
    apply_limits(st, rate_global_per_s=1)
    yield _ev("step", text=f"Global limit temporarily lowered to 1 request/s (burst 2). {fans} different fans send "
                           f"{n} questions at once. Each fan is under its own limit, so any 429 here comes from the global throttle.")
    try:
        tokens = [_token(st) for _ in range(fans)]
        results = await asyncio.gather(*(_ask(client, tokens[i % fans], "what's the score?") for i in range(n)))
        for i, r in enumerate(results):
            r["i"] = i + 1
            yield _ev("request", **r)
    finally:
        apply_limits(st, **saved)
        yield _ev("step", text=f"Global limit restored to {saved['rate_global_per_s']}/s.")
    ok = sum(1 for r in results if r["status"] == 200)
    throttled = sum(1 for r in results if r["status"] == 429 and r["scope"] == "global")
    passed = 1 <= ok <= 3 and throttled >= n - 3
    yield _ev("verdict", passed=passed,
              summary=f"{ok} allowed, {throttled} throttled by the global limit. "
                      + ("Global throttling works." if passed else "Expected almost all requests to be throttled."))


async def _load_shed(st, client) -> AsyncIterator[dict]:
    if st.health.level >= 2:
        yield _ev("verdict", passed=False, summary="The chat model is offline (level 2), so nothing reaches the LLM queue. Restore it and rerun.")
        return
    saved = current_limits(st)
    n = 6
    apply_limits(st, llm_queue_max=1, llm_queue_timeout_s=60)
    yield _ev("step", text=f"LLM queue temporarily set to 1 waiting place ({st.admission.max_concurrency} running). "
                           f"{n} fans ask different AI questions at once. Expected: 1 runs, 1 waits, the rest are shed "
                           f"to a quick data answer immediately instead of waiting.")
    topics = ["the LBW rule", "a free hit", "the powerplay", "net run rate", "a super over", "the DLS method"]
    nonce = secrets.token_hex(2)  # keeps the answer cache out of the way
    try:
        results = await asyncio.gather(*(_ask(client, _token(st), f"Explain {topics[i % len(topics)]} in one line (test {nonce}{i})")
                                         for i in range(n)))
    finally:
        apply_limits(st, **saved)
    for i, r in enumerate(sorted(results, key=lambda r: r["ms"])):
        r["i"] = i + 1
        yield _ev("request", **r)
    yield _ev("step", text=f"Queue restored to {saved['llm_queue_max']}.")
    shed = [r for r in results if r["path"] == "shed"]
    llm = [r for r in results if r["path"] == "llm"]
    errors = [r for r in results if r["status"] != 200]
    fast = max((r["ms"] for r in shed), default=0)
    passed = len(shed) >= n - 2 and len(llm) >= 1 and not errors
    yield _ev("verdict", passed=passed,
              summary=f"{len(llm)} answered by the LLM, {len(shed)} shed (slowest shed answer {fast} ms), {len(errors)} errors. "
                      + ("Load shedding works: excess questions were answered fast instead of queueing." if passed
                         else "Expected most requests to be shed quickly and at least one LLM answer."))
