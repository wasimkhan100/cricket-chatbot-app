"""Component health, degrade level and chaos switches.

Degrade levels (each one keeps the levels below it working):
  0 normal        all features on
  1 busy          LLM queue over half full: thinking off, shorter answers
  2 llm_offline   chat model unreachable: templates, cached answers and direct data answers only
Knowledge search (Chroma + embeddings) and the stats DB degrade independently: the agent simply loses that tool.
"""

import time
from dataclasses import dataclass, field

from ..observability.metrics import COMPONENT_UP, DEGRADE_LEVEL
from .circuit_breaker import CircuitBreaker, State
from .load_shed import AdmissionController

LEVEL_NAMES = {0: "normal", 1: "busy", 2: "llm_offline"}
CHAOS_TARGETS = ("ollama_chat", "ollama_embed", "chroma", "stats_db", "feed")


class ChaosError(RuntimeError):
    """Raised by a dependency when an operator has switched on failure injection for it."""


@dataclass
class HealthRegistry:
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    admission: AdmissionController | None = None
    chaos: set[str] = field(default_factory=set)
    forced_level: int | None = None
    feed_last_ball_at: float | None = None
    feed_running: bool = False
    component_notes: dict[str, str] = field(default_factory=dict)

    def breaker(self, name: str, **kw) -> CircuitBreaker:
        if name not in self.breakers:
            self.breakers[name] = CircuitBreaker(name, **kw)
        return self.breakers[name]

    def check_chaos(self, target: str) -> None:
        if target in self.chaos:
            raise ChaosError(f"failure injected for {target}")

    def is_up(self, name: str) -> bool:
        if name in self.chaos:
            return False
        b = self.breakers.get(name)
        return b is None or b.state is not State.OPEN

    @property
    def level(self) -> int:
        if self.forced_level is not None:
            lvl = self.forced_level
        elif not self.is_up("ollama_chat"):
            lvl = 2
        elif self.admission and self.admission.pressure >= 0.5:
            lvl = 1
        else:
            lvl = 0
        DEGRADE_LEVEL.set(lvl)
        return lvl

    def components(self) -> list[dict]:
        out = []
        for name in CHAOS_TARGETS:
            b = self.breakers.get(name)
            up = self.is_up(name)
            if name == "feed":
                lag = None if self.feed_last_ball_at is None else time.time() - self.feed_last_ball_at
                note = "replay stopped" if not self.feed_running else (f"last ball {lag:.0f}s ago" if lag is not None else "waiting")
            else:
                note = self.component_notes.get(name, "")
            COMPONENT_UP.labels(name).set(1 if up else 0)
            out.append({
                "name": name,
                "up": up,
                "breaker": b.state.value if b else "n/a",
                "chaos": name in self.chaos,
                "last_error": b.last_error if b else None,
                "note": note,
            })
        return out

    def snapshot(self) -> dict:
        lvl = self.level
        return {
            "degrade_level": lvl,
            "degrade_name": LEVEL_NAMES[lvl],
            "forced": self.forced_level is not None,
            "components": self.components(),
            "admission": self.admission.snapshot() if self.admission else None,
        }
