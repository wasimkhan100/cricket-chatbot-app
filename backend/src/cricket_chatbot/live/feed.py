"""Live feed replay and fan-out.

The replay plays a real match from deliveries.csv one ball every few seconds, standing in for a
ball-by-ball data provider. State changes are broadcast to ticker subscribers through bounded queues:
a slow browser drops old updates instead of slowing the feed down.
"""

import asyncio
import logging
import time

from ..data.stats_db import StatsDB
from ..observability.metrics import FEED_BALLS, FEED_LAG, SSE_CLIENTS
from ..resilience.health import ChaosError, HealthRegistry
from .state import BallEvent, MatchState, StateBuilder

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self, queue_size: int = 4):
        self._subs: set[asyncio.Queue] = set()
        self._size = queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._size)
        self._subs.add(q)
        SSE_CLIENTS.set(len(self._subs))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        SSE_CLIENTS.set(len(self._subs))

    def publish(self, item: dict) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(item)

    @property
    def count(self) -> int:
        return len(self._subs)


def ball_commentary(ev: BallEvent, over_str: str) -> str:
    who = f"{ev.bowler} to {ev.batsman}"
    if ev.player_dismissed:
        kind = ev.dismissal_kind
        extra = f", {ev.fielder} takes it" if kind == "caught" and ev.fielder else ""
        return f"{over_str} {who}. OUT! {ev.player_dismissed} is {kind}{extra}."
    if ev.batsman_runs == 6:
        return f"{over_str} {who}. SIX! Cleared the ropes."
    if ev.batsman_runs == 4:
        return f"{over_str} {who}. FOUR! Raced away to the boundary."
    if ev.wide_runs:
        return f"{over_str} {who}. Wide, {ev.total_runs} run(s) added."
    if ev.noball_runs:
        return f"{over_str} {who}. No ball! Free hit coming. {ev.total_runs} run(s)."
    if ev.bye_runs or ev.legbye_runs:
        return f"{over_str} {who}. {ev.extra_runs} {'bye' if ev.bye_runs else 'leg bye'}(s)."
    if ev.total_runs == 0:
        return f"{over_str} {who}. Dot ball."
    return f"{over_str} {who}. {ev.total_runs} run{'s' if ev.total_runs > 1 else ''}."


class LiveFeed:
    def __init__(self, stats: StatsDB, match_id: int, interval_s: float, health: HealthRegistry):
        self.stats = stats
        self.match_id = match_id
        self.interval_s = interval_s
        self.health = health
        self.broadcaster = Broadcaster()
        self._events: list[BallEvent] = []
        self._cursor = 0
        self._task: asyncio.Task | None = None
        self._paused = False
        self.builder = self._new_builder()

    def _new_builder(self) -> StateBuilder:
        m = self.stats.match(self.match_id)
        if not m:
            raise ValueError(f"match {self.match_id} not found")
        title = f"{m['team1']} vs {m['team2']}"
        state = MatchState(match_id=m["id"], season=m["season"], venue=m["venue"], team1=m["team1"], team2=m["team2"],
                           toss=f"{m['toss_winner']} won the toss and chose to {m['toss_decision']}", title=title)
        self._match_row = m
        self._events = [
            BallEvent(seq=i + 1, inning=d["inning"], over=d["over"], ball=d["ball"], batting_team=d["batting_team"],
                      bowling_team=d["bowling_team"], batsman=d["batsman"], non_striker=d["non_striker"], bowler=d["bowler"],
                      batsman_runs=d["batsman_runs"], extra_runs=d["extra_runs"], total_runs=d["total_runs"],
                      wide_runs=d["wide_runs"], noball_runs=d["noball_runs"], bye_runs=d["bye_runs"],
                      legbye_runs=d["legbye_runs"], player_dismissed=d["player_dismissed"] or "",
                      dismissal_kind=d["dismissal_kind"] or "", fielder=d["fielder"] or "")
            for i, d in enumerate(self.stats.deliveries(self.match_id))
        ]
        self._cursor = 0
        return StateBuilder(state)

    @property
    def state(self) -> MatchState:
        return self.builder.state

    def snapshot(self) -> MatchState:
        return self.builder.snapshot()

    @property
    def progress(self) -> dict:
        return {"ball": self._cursor, "of": len(self._events), "paused": self._paused, "interval_s": self.interval_s}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live-feed")
            self.health.feed_running = True

    async def stop(self) -> None:
        self.health.feed_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def pause(self, paused: bool) -> None:
        self._paused = paused

    def set_speed(self, interval_s: float) -> None:
        self.interval_s = max(0.2, min(interval_s, 30.0))

    async def restart(self, match_id: int | None = None) -> None:
        await self.stop()
        if match_id:
            self.match_id = match_id
        self.builder = self._new_builder()
        self._publish()
        self.start()

    def apply_next(self) -> bool:
        """Applies one delivery. Returns False when the match is over."""
        if self._cursor >= len(self._events):
            return False
        ev = self._events[self._cursor]
        self._cursor += 1
        if self.builder.apply(ev):
            FEED_BALLS.inc()
            s = self.builder.state
            s.commentary.insert(0, {"over": s.current.overs, "text": ball_commentary(ev, f"{ev.over - 1}.{ev.ball}"),
                                    "kind": "wicket" if ev.player_dismissed else ("boundary" if ev.batsman_runs >= 4 else "ball"),
                                    "version": s.version})
            del s.commentary[40:]
            self.health.feed_last_ball_at = time.time()
        if self._cursor >= len(self._events):
            self.builder.finish(self._result_text())
        self._publish()
        return True

    def _result_text(self) -> str:
        m = self._match_row
        if not m["winner"]:
            return "No result"
        margin = f"{m['win_by_runs']} run{'s' if m['win_by_runs'] != 1 else ''}" if m["win_by_runs"] else \
            f"{m['win_by_wickets']} wicket{'s' if m['win_by_wickets'] != 1 else ''}"
        pom = f" Player of the match: {m['player_of_match']}." if m["player_of_match"] else ""
        return f"{m['winner']} won by {margin}.{pom}"

    def _publish(self) -> None:
        self.broadcaster.publish(self.builder.state.to_public())

    async def _run(self) -> None:
        log.info("live feed started", extra={"match_id": self.match_id, "balls": len(self._events)})
        try:
            while True:
                await asyncio.sleep(self.interval_s)
                last = self.health.feed_last_ball_at
                FEED_LAG.set(time.time() - last if last else 0)
                if self._paused:
                    continue
                try:
                    self.health.check_chaos("feed")
                except ChaosError:
                    continue  # the provider has gone quiet; state stays put and the UI shows it as stale
                if not self.apply_next():
                    log.info("live feed finished", extra={"match_id": self.match_id})
                    self.health.feed_running = False
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live feed crashed")
            self.health.feed_running = False
