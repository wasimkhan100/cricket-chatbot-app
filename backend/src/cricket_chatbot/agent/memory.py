"""Answer cache, request coalescing and per-session conversation memory. All bounded in size and time."""

import asyncio
import re
import time
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable


FOLLOW_UP = re.compile(r"^\s*(and|also|what about|how about|same for)\b|\b(he|his|him|she|her|they|their|them)\b"
                       r"|\b(that|this|same) (season|year|match|game|innings|player|team|over)\b", re.I)


def is_follow_up(question: str, history: list[dict]) -> bool:
    """A question that only makes sense with the previous turn ("and his strike rate?")."""
    return bool(history) and len(question.split()) <= 14 and bool(FOLLOW_UP.search(question))


def normalise_question(q: str) -> str:
    q = q.lower()
    q = re.sub(r"[^\w\s]", " ", q)
    q = re.sub(r"\b(please|pls|plz|bro|yaar|bhai|hey|hi|the|a|an|tell me|can you|could you)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


class TTLCache:
    def __init__(self, max_items: int, ttl_s: float):
        self.max_items = max_items
        self.ttl_s = ttl_s
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        item = self._d.get(key)
        if item is None or time.monotonic() - item[0] > self.ttl_s:
            self._d.pop(key, None)
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return item[1]

    def put(self, key: str, value: Any) -> None:
        self._d[key] = (time.monotonic(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()

    def snapshot(self) -> dict:
        total = self.hits + self.misses
        return {"size": len(self._d), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}


class SingleFlight:
    """Concurrent identical questions share one in-flight computation (stampede protection after a wicket)."""

    def __init__(self):
        self._inflight: dict[str, asyncio.Future] = {}

    def leader(self, key: str) -> tuple[bool, asyncio.Future]:
        fut = self._inflight.get(key)
        if fut is not None:
            return False, fut
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        return True, fut

    def finish(self, key: str, result: Any = None, error: BaseException | None = None) -> None:
        fut = self._inflight.pop(key, None)
        if fut and not fut.done():
            if error:
                fut.set_exception(error)
                fut.exception()  # mark retrieved so a follower-less error doesn't log a warning
            else:
                fut.set_result(result)

    async def run(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        is_leader, fut = self.leader(key)
        if not is_leader:
            return await asyncio.shield(fut)
        try:
            result = await fn()
        except BaseException as e:
            self.finish(key, error=e)
            raise
        self.finish(key, result)
        return result


class SessionMemory:
    def __init__(self, max_sessions: int = 10_000, max_turns: int = 6, ttl_s: float = 3600):
        self._d: OrderedDict[str, tuple[float, deque]] = OrderedDict()
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self.ttl_s = ttl_s

    def history(self, sid: str) -> list[dict]:
        item = self._d.get(sid)
        if not item or time.monotonic() - item[0] > self.ttl_s:
            return []
        return list(item[1])

    def append(self, sid: str, question: str, answer: str) -> None:
        _, turns = self._d.get(sid, (0.0, deque(maxlen=self.max_turns * 2)))
        turns.append({"role": "user", "content": question})
        turns.append({"role": "assistant", "content": answer[:800]})
        self._d[sid] = (time.monotonic(), turns)
        self._d.move_to_end(sid)
        while len(self._d) > self.max_sessions:
            self._d.popitem(last=False)

    def clear(self, sid: str) -> None:
        self._d.pop(sid, None)

    def __len__(self) -> int:
        return len(self._d)
