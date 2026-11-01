"""Token-bucket rate limiting, per client and global (throttling)."""

import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Callable


@dataclass
class Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    """`rate` tokens per second refill, up to `burst`. Bounded memory: least recently seen keys are evicted."""

    def __init__(self, rate_per_s: float, burst: float, max_keys: int = 50_000,
                 clock: Callable[[], float] = time.monotonic):
        self.rate = rate_per_s
        self.burst = burst
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, Bucket] = OrderedDict()
        self._lock = Lock()

    def acquire(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = Bucket(self.burst, now)
                self._buckets[key] = b
                if len(self._buckets) > self.max_keys:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
                b.tokens = min(self.burst, b.tokens + (now - b.updated) * self.rate)
                b.updated = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True, 0.0
            return False, (cost - b.tokens) / self.rate if self.rate > 0 else 60.0
