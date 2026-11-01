"""Versioned live match state, built by a single writer from ball events.

Every applied delivery bumps `version`. Answers and cache entries carry the version they were built from,
so a fan always knows how fresh a figure is ("as of over 14.3").
"""

import copy
import time
from dataclasses import asdict, dataclass, field

NOT_BOWLER_WICKETS = {"run out", "retired hurt", "obstructing the field", ""}


@dataclass
class BallEvent:
    seq: int            # monotonically increasing per match; used for dedupe and ordering
    inning: int
    over: int           # 1-based, as in the feed
    ball: int
    batting_team: str
    bowling_team: str
    batsman: str
    non_striker: str
    bowler: str
    batsman_runs: int
    extra_runs: int
    total_runs: int
    wide_runs: int
    noball_runs: int
    bye_runs: int
    legbye_runs: int
    player_dismissed: str = ""
    dismissal_kind: str = ""
    fielder: str = ""

    @property
    def legal(self) -> bool:
        return self.wide_runs == 0 and self.noball_runs == 0


@dataclass
class BatterLine:
    name: str
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    out: str = ""

    @property
    def strike_rate(self) -> float:
        return round(100 * self.runs / self.balls, 1) if self.balls else 0.0


@dataclass
class BowlerLine:
    name: str
    balls: int = 0
    runs: int = 0
    wickets: int = 0

    @property
    def overs(self) -> str:
        return f"{self.balls // 6}.{self.balls % 6}"

    @property
    def economy(self) -> float:
        return round(6 * self.runs / self.balls, 2) if self.balls else 0.0


@dataclass
class Innings:
    number: int
    batting_team: str
    bowling_team: str
    runs: int = 0
    wickets: int = 0
    legal_balls: int = 0
    extras: int = 0
    fours: int = 0
    sixes: int = 0
    batters: dict[str, BatterLine] = field(default_factory=dict)
    bowlers: dict[str, BowlerLine] = field(default_factory=dict)
    fall_of_wickets: list[str] = field(default_factory=list)
    over_runs: list[int] = field(default_factory=list)

    @property
    def overs(self) -> str:
        return f"{self.legal_balls // 6}.{self.legal_balls % 6}"

    @property
    def run_rate(self) -> float:
        return round(6 * self.runs / self.legal_balls, 2) if self.legal_balls else 0.0


@dataclass
class MatchState:
    match_id: int
    season: int
    venue: str
    team1: str
    team2: str
    toss: str
    title: str
    version: int = 0
    last_seq: int = 0
    innings: list[Innings] = field(default_factory=list)
    striker: str = ""
    non_striker: str = ""
    current_bowler: str = ""
    last_balls: list[str] = field(default_factory=list)   # e.g. ["1", "4", "W", "1wd"]
    last_event: str = "Match about to start"
    commentary: list[dict] = field(default_factory=list)  # newest first
    status: str = "scheduled"                             # scheduled | live | innings_break | complete
    result: str = ""
    updated_at: float = 0.0
    max_overs: int = 20

    # --- derived figures -------------------------------------------------------
    @property
    def current(self) -> Innings | None:
        return self.innings[-1] if self.innings else None

    @property
    def target(self) -> int | None:
        return self.innings[0].runs + 1 if len(self.innings) >= 2 else None

    def chase(self) -> dict | None:
        inn = self.current
        if not inn or inn.number != 2 or self.target is None:
            return None
        need = self.target - inn.runs
        balls_left = self.max_overs * 6 - inn.legal_balls
        return {
            "target": self.target,
            "runs_needed": max(need, 0),
            "balls_left": max(balls_left, 0),
            "required_run_rate": round(6 * need / balls_left, 2) if balls_left > 0 and need > 0 else 0.0,
        }

    def as_of(self) -> str:
        inn = self.current
        return f"v{self.version} · inns {inn.number} · over {inn.overs}" if inn else f"v{self.version} · pre-match"

    def to_public(self) -> dict:
        """Compact JSON for the ticker and the UI."""
        d = {
            "match_id": self.match_id, "title": self.title, "season": self.season, "venue": self.venue,
            "team1": self.team1, "team2": self.team2, "toss": self.toss, "version": self.version, "status": self.status,
            "result": self.result, "last_event": self.last_event, "last_balls": self.last_balls[-12:],
            "striker": self.striker, "non_striker": self.non_striker, "bowler": self.current_bowler,
            "updated_at": self.updated_at, "as_of": self.as_of(), "chase": self.chase(),
            "commentary": self.commentary[:15], "innings": [],
        }
        for inn in self.innings:
            d["innings"].append({
                "number": inn.number, "batting_team": inn.batting_team, "bowling_team": inn.bowling_team,
                "runs": inn.runs, "wickets": inn.wickets, "overs": inn.overs, "run_rate": inn.run_rate,
                "extras": inn.extras, "fours": inn.fours, "sixes": inn.sixes, "fall_of_wickets": inn.fall_of_wickets,
                "batters": [{**asdict(b), "strike_rate": b.strike_rate} for b in inn.batters.values()],
                "bowlers": [{**asdict(b), "overs": b.overs, "economy": b.economy} for b in inn.bowlers.values()],
            })
        return d

    def to_prompt_context(self) -> str:
        """The match snapshot the LLM sees. Plain text, compact, every figure labelled."""
        lines = [f"MATCH: {self.title} | {self.venue} | season {self.season}", f"TOSS: {self.toss}",
                 f"STATUS: {self.status}" + (f" | RESULT: {self.result}" if self.result else ""),
                 f"AS OF: {self.as_of()}"]
        for inn in self.innings:
            lines.append(f"INNINGS {inn.number}: {inn.batting_team} {inn.runs}/{inn.wickets} in {inn.overs} overs "
                         f"(run rate {inn.run_rate}, extras {inn.extras}, fours {inn.fours}, sixes {inn.sixes})")
            bats = sorted(inn.batters.values(), key=lambda b: -b.runs)[:6]
            lines.append("  Batting: " + "; ".join(
                f"{b.name} {b.runs}({b.balls}){' ' + b.out if b.out else ' not out'}" for b in bats))
            bowls = sorted(inn.bowlers.values(), key=lambda b: (-b.wickets, b.runs))[:5]
            lines.append("  Bowling: " + "; ".join(f"{b.name} {b.overs}-{b.runs}-{b.wickets}" for b in bowls))
            if inn.fall_of_wickets:
                lines.append("  Fall of wickets: " + ", ".join(inn.fall_of_wickets[-6:]))
        if self.status == "live":
            lines.append(f"AT THE CREASE: {self.striker} (on strike), {self.non_striker}; bowling: {self.current_bowler}")
            lines.append("LAST BALLS: " + " ".join(self.last_balls[-12:]))
        if c := self.chase():
            lines.append(f"CHASE: target {c['target']}, need {c['runs_needed']} from {c['balls_left']} balls, "
                         f"required run rate {c['required_run_rate']}")
        lines.append(f"LAST EVENT: {self.last_event}")
        return "\n".join(lines)

    def to_brief(self) -> str:
        """One-paragraph snapshot for questions that aren't about the live match (smaller prompt, faster on CPU)."""
        parts = [f"Live now: {self.title} ({self.status})"]
        for inn in self.innings:
            parts.append(f"{inn.batting_team} {inn.runs}/{inn.wickets} in {inn.overs} overs")
        if self.result:
            parts.append(self.result)
        return f"AS OF: {self.as_of()}\n" + " | ".join(parts)

    def known_numbers(self) -> set[int]:
        """Every integer the snapshot states. The validator only accepts answer numbers from this set (plus small ones)."""
        import re
        nums = {int(n) for n in re.findall(r"\d+", self.to_prompt_context())}
        for inn in self.innings:
            nums.update({inn.runs, inn.wickets, inn.legal_balls // 6, inn.extras})
        return nums


class StateBuilder:
    """Single writer. Idempotent: replayed or duplicate events (seq <= last_seq) are ignored."""

    def __init__(self, state: MatchState):
        self._state = state

    @property
    def state(self) -> MatchState:
        return self._state

    def snapshot(self) -> MatchState:
        return copy.deepcopy(self._state)

    def apply(self, ev: BallEvent) -> bool:
        s = self._state
        if ev.seq <= s.last_seq:
            return False
        if not s.innings or s.current.number != ev.inning:
            if s.innings:
                s.commentary.insert(0, {"over": s.current.overs, "text": f"Innings break. {s.current.batting_team} "
                                        f"finish on {s.current.runs}/{s.current.wickets}. Target {s.current.runs + 1}.",
                                        "kind": "break", "version": s.version + 1})
            s.innings.append(Innings(ev.inning, ev.batting_team, ev.bowling_team))
            s.last_balls = []
        inn = s.current
        s.status = "live"

        bat = inn.batters.setdefault(ev.batsman, BatterLine(ev.batsman))
        inn.batters.setdefault(ev.non_striker, BatterLine(ev.non_striker))
        bowl = inn.bowlers.setdefault(ev.bowler, BowlerLine(ev.bowler))

        inn.runs += ev.total_runs
        inn.extras += ev.extra_runs
        bat.runs += ev.batsman_runs
        if ev.wide_runs == 0:
            bat.balls += 1
        if ev.batsman_runs == 4:
            bat.fours += 1
            inn.fours += 1
        elif ev.batsman_runs == 6:
            bat.sixes += 1
            inn.sixes += 1
        bowl.runs += ev.total_runs - ev.bye_runs - ev.legbye_runs
        if ev.legal:
            inn.legal_balls += 1
            bowl.balls += 1
            if inn.legal_balls % 6 == 0:
                inn.over_runs.append(inn.runs - sum(inn.over_runs))

        symbol = str(ev.total_runs)
        if ev.wide_runs:
            symbol = f"{ev.total_runs}wd"
        elif ev.noball_runs:
            symbol = f"{ev.total_runs}nb"
        if ev.player_dismissed:
            inn.wickets += 1
            if ev.dismissal_kind not in NOT_BOWLER_WICKETS:
                bowl.wickets += 1
            out_line = inn.batters.setdefault(ev.player_dismissed, BatterLine(ev.player_dismissed))
            how = ev.dismissal_kind + (f" ({ev.fielder})" if ev.fielder else "")
            out_line.out = f"{how} b {ev.bowler}" if ev.dismissal_kind not in NOT_BOWLER_WICKETS else how
            inn.fall_of_wickets.append(f"{inn.runs}-{inn.wickets} ({ev.player_dismissed}, {inn.overs})")
            symbol = "W"
        s.last_balls.append(symbol)

        s.striker, s.non_striker, s.current_bowler = ev.batsman, ev.non_striker, ev.bowler
        if ev.player_dismissed:
            s.last_event = f"WICKET! {ev.player_dismissed} {ev.dismissal_kind}" + (f" by {ev.fielder}" if ev.fielder else "") + f", bowled by {ev.bowler}"
        elif ev.batsman_runs == 6:
            s.last_event = f"SIX! {ev.batsman} off {ev.bowler}"
        elif ev.batsman_runs == 4:
            s.last_event = f"FOUR! {ev.batsman} off {ev.bowler}"
        elif ev.extra_runs:
            s.last_event = f"{ev.extra_runs} extra run(s) from {ev.bowler}"
        else:
            s.last_event = f"{ev.bowler} to {ev.batsman}, {ev.batsman_runs} run(s)"

        s.last_seq = ev.seq
        s.version += 1
        s.updated_at = time.time()
        return True

    def finish(self, result: str) -> None:
        self._state.status = "complete"
        self._state.result = result
        self._state.last_event = result
        self._state.version += 1
        self._state.updated_at = time.time()

    def add_commentary(self, version: int, over: str, text: str, kind: str = "ball") -> None:
        self._state.commentary.insert(0, {"over": over, "text": text, "kind": kind, "version": version})
        del self._state.commentary[40:]
