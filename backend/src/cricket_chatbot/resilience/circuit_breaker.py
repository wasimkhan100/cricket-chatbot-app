"""Circuit breaker: stop calling a dependency that keeps failing, probe it again after a cool-down."""

import asyncio
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

from ..observability.metrics import BREAKER_STATE
from ..observability.tracing import record_error

T = TypeVar("T")


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_in: float):
        super().__init__(f"{name} unavailable (circuit open, retry in {retry_in:.0f}s)")
        self.name = name
        self.retry_in = retry_in


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, reset_after_s: float = 20.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._state = State.CLOSED
        self._probe_lock = asyncio.Lock()
        self.last_error: str | None = None
        BREAKER_STATE.labels(name).set(0)

    @property
    def state(self) -> State:
        if self._state is State.OPEN and self._clock() - self._opened_at >= self.reset_after_s:
            return State.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        return self.state is not State.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._state = State.CLOSED
        BREAKER_STATE.labels(self.name).set(0)

    def record_failure(self, err: BaseException) -> None:
        self._failures += 1
        self.last_error = f"{type(err).__name__}: {err}"[:300]
        record_error(self.name, self.last_error)
        if self.state is State.HALF_OPEN or self._failures >= self.failure_threshold:
            self._state = State.OPEN
            self._opened_at = self._clock()
            BREAKER_STATE.labels(self.name).set(1)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        state = self.state
        if state is State.OPEN:
            raise CircuitOpenError(self.name, self.reset_after_s - (self._clock() - self._opened_at))
        if state is State.HALF_OPEN:
            # Only one probe at a time; everyone else fails fast until it succeeds.
            if self._probe_lock.locked():
                raise CircuitOpenError(self.name, 1)
            async with self._probe_lock:
                return await self._run(fn)
        return await self._run(fn)

    async def _run(self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.record_failure(e)
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state.value, "failures": self._failures, "last_error": self.last_error}
