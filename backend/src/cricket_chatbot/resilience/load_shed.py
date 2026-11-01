"""Admission control for the LLM: bounded concurrency, bounded queue, bounded wait.

Requests beyond the queue limit are shed immediately instead of piling up and timing out for everyone.
"""

import asyncio
import contextlib

from ..observability.metrics import LLM_INFLIGHT, LLM_QUEUE, SHED


class Overloaded(Exception):
    def __init__(self, reason: str, retry_after: float = 5.0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class AdmissionController:
    def __init__(self, max_concurrency: int, max_queue: int, queue_timeout_s: float):
        self._sem = asyncio.Semaphore(max_concurrency)
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self.queue_timeout_s = queue_timeout_s
        self.waiting = 0
        self.inflight = 0

    @property
    def pressure(self) -> float:
        """0..1+, how full the queue is. Used to pick the degrade level."""
        return self.waiting / self.max_queue if self.max_queue else 0.0

    @contextlib.asynccontextmanager
    async def slot(self):
        if self.waiting >= self.max_queue:
            SHED.labels("queue_full").inc()
            raise Overloaded("queue_full", retry_after=10)
        self.waiting += 1
        LLM_QUEUE.set(self.waiting)
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.queue_timeout_s)
        except TimeoutError:
            SHED.labels("queue_timeout").inc()
            raise Overloaded("queue_timeout", retry_after=10) from None
        finally:
            self.waiting -= 1
            LLM_QUEUE.set(self.waiting)
        self.inflight += 1
        LLM_INFLIGHT.set(self.inflight)
        try:
            yield
        finally:
            self.inflight -= 1
            LLM_INFLIGHT.set(self.inflight)
            self._sem.release()

    def snapshot(self) -> dict:
        return {"inflight": self.inflight, "waiting": self.waiting,
                "max_concurrency": self.max_concurrency, "max_queue": self.max_queue}
