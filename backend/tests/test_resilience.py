import asyncio

import pytest

from cricket_chatbot.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, State
from cricket_chatbot.resilience.health import HealthRegistry
from cricket_chatbot.resilience.load_shed import AdmissionController, Overloaded
from cricket_chatbot.resilience.rate_limit import TokenBucketLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def _boom():
    raise RuntimeError("down")


async def _ok():
    return "ok"


async def test_breaker_opens_after_threshold_and_recovers_via_half_open():
    clock = Clock()
    b = CircuitBreaker("dep", failure_threshold=2, reset_after_s=10, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await b.call(_boom)
    assert b.state is State.OPEN
    with pytest.raises(CircuitOpenError):
        await b.call(_ok)          # fails fast, dependency not called
    clock.t = 10
    assert b.state is State.HALF_OPEN
    assert await b.call(_ok) == "ok"
    assert b.state is State.CLOSED


async def test_breaker_half_open_failure_reopens():
    clock = Clock()
    b = CircuitBreaker("dep", failure_threshold=1, reset_after_s=5, clock=clock)
    with pytest.raises(RuntimeError):
        await b.call(_boom)
    clock.t = 5
    with pytest.raises(RuntimeError):
        await b.call(_boom)
    assert b.state is State.OPEN


def test_token_bucket_burst_then_refill():
    clock = Clock()
    lim = TokenBucketLimiter(rate_per_s=1, burst=3, clock=clock)
    assert [lim.acquire("fan")[0] for _ in range(4)] == [True, True, True, False]
    ok, retry = lim.acquire("fan")
    assert not ok and retry == pytest.approx(1.0)
    assert lim.acquire("other-fan")[0]   # limits are per key
    clock.t = 2
    assert lim.acquire("fan")[0] and lim.acquire("fan")[0] and not lim.acquire("fan")[0]


def test_token_bucket_memory_is_bounded():
    lim = TokenBucketLimiter(1, 1, max_keys=100)
    for i in range(1000):
        lim.acquire(f"k{i}")
    assert len(lim._buckets) == 100


async def test_admission_sheds_when_queue_is_full():
    ac = AdmissionController(max_concurrency=1, max_queue=1, queue_timeout_s=5)
    release = asyncio.Event()

    async def hold():
        async with ac.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(hold())   # takes the one queue place
    await asyncio.sleep(0)
    with pytest.raises(Overloaded) as e:
        async with ac.slot():
            pass
    assert e.value.reason == "queue_full"
    release.set()
    await asyncio.gather(holder, waiter)
    assert ac.inflight == 0 and ac.waiting == 0


async def test_admission_queue_timeout():
    ac = AdmissionController(max_concurrency=1, max_queue=5, queue_timeout_s=0.05)
    async with ac.slot():
        with pytest.raises(Overloaded) as e:
            async with ac.slot():
                pass
    assert e.value.reason == "queue_timeout"


async def test_degrade_level_follows_llm_health_and_pressure():
    h = HealthRegistry()
    h.breaker("ollama_chat")
    h.admission = AdmissionController(1, 4, 1)
    assert h.level == 0
    h.admission.waiting = 2
    assert h.level == 1
    h.chaos.add("ollama_chat")
    assert h.level == 2
    h.chaos.clear()
    h.admission.waiting = 0
    h.forced_level = 1
    assert h.level == 1


def test_monitor_fires_and_resolves_alerts():
    from types import SimpleNamespace

    from cricket_chatbot.agent.memory import TTLCache
    from cricket_chatbot.observability.history import Monitor

    h = HealthRegistry()
    h.breaker("ollama_chat")
    h.admission = AdmissionController(1, 4, 1)
    app = SimpleNamespace(state=SimpleNamespace(health=h, admission=h.admission, cache=TTLCache(10, 60),
                                                feed=SimpleNamespace(broadcaster=SimpleNamespace(count=0))))
    m = Monitor()
    m.sample(app)
    assert m.alerts()["overall"] == "healthy"
    h.chaos.add("chroma")
    m.sample(app)
    a = m.alerts()
    assert a["overall"] == "down" and a["active"][0]["key"] == "down:chroma"
    h.chaos.clear()
    m.sample(app)
    a = m.alerts()
    assert a["overall"] == "healthy" and a["history"][0]["event"] == "resolved"
