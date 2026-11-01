"""Read-only tools the agent can call. Arguments are validated here; the model never touches SQL or the filesystem."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..data.stats_db import StatsDB
from ..live.state import MatchState
from ..observability.agent_runs import observer
from ..observability.metrics import TOOL_CALLS
from ..observability.tracing import tracer
from ..rag.store import KnowledgeStore, KnowledgeUnavailable
from ..resilience.circuit_breaker import CircuitBreaker, CircuitOpenError
from ..resilience.health import HealthRegistry

MAX_RESULT_CHARS = 1800


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    stats: StatsDB
    knowledge: KnowledgeStore
    snapshot: MatchState
    health: HealthRegistry
    stats_breaker: CircuitBreaker
    sources: list[dict] = field(default_factory=list)   # what the answer was grounded on, shown in the UI
    numbers: set[int] = field(default_factory=set)      # numbers seen in tool results, for the validator
    evidence: list[dict] = field(default_factory=list)  # the text behind each source, for offline evaluation


def _season(v: Any) -> int | None:
    if v in (None, "", 0, "all"):
        return None
    try:
        s = int(str(v)[:4])
    except ValueError:
        raise ToolError("season must be a year between 2008 and 2017") from None
    if not 2008 <= s <= 2017:
        raise ToolError(f"no data for {s}: the dataset covers IPL 2008-2017")
    return s


def _text(v: Any, name: str, max_len: int = 60) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ToolError(f"{name} is required")
    return v.strip()[:max_len]


async def _stats_call(ctx: ToolContext, fn: Callable[[], Any]) -> Any:
    async def go():
        ctx.health.check_chaos("stats_db")
        return await asyncio.to_thread(fn)

    try:
        return await ctx.stats_breaker.call(go)
    except CircuitOpenError:
        raise ToolError("the stats database is temporarily unavailable") from None
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"stats lookup failed: {type(e).__name__}") from e


def _resolve_player(ctx: ToolContext, name: str) -> str:
    cands = ctx.stats.resolve_player(name)
    if not cands:
        raise ToolError(f"no IPL player found matching '{name}'")
    return cands[0]


def _resolve_team(ctx: ToolContext, name: str) -> str:
    t = ctx.stats.resolve_team(name)
    if not t:
        raise ToolError(f"no IPL team found matching '{name}'")
    return t


# --- handlers -------------------------------------------------------------------
async def get_live_match(ctx: ToolContext, args: dict) -> dict:
    ctx.sources.append({"type": "live", "id": ctx.snapshot.as_of()})
    return {"snapshot": ctx.snapshot.to_prompt_context()}


async def get_player_stats(ctx: ToolContext, args: dict) -> dict:
    name = _text(args.get("player"), "player")
    role = (args.get("role") or "batting").lower()
    season = _season(args.get("season"))
    player = await _stats_call(ctx, lambda: _resolve_player(ctx, name))
    if role.startswith("bowl"):
        out = await _stats_call(ctx, lambda: ctx.stats.bowling(player, season))
    elif role == "both":
        out = {"batting": await _stats_call(ctx, lambda: ctx.stats.batting(player, season)),
               "bowling": await _stats_call(ctx, lambda: ctx.stats.bowling(player, season))}
    else:
        out = await _stats_call(ctx, lambda: ctx.stats.batting(player, season))
    ctx.sources.append({"type": "stats", "id": f"{player} {role} {season or 'career'}"})
    return out


async def head_to_head(ctx: ToolContext, args: dict) -> dict:
    a = await _stats_call(ctx, lambda: _resolve_team(ctx, _text(args.get("team_a"), "team_a")))
    b = await _stats_call(ctx, lambda: _resolve_team(ctx, _text(args.get("team_b"), "team_b")))
    ctx.sources.append({"type": "stats", "id": f"{a} v {b}"})
    return await _stats_call(ctx, lambda: ctx.stats.head_to_head(a, b))


async def season_summary(ctx: ToolContext, args: dict) -> dict:
    season = _season(args.get("season"))
    if season is None:
        raise ToolError("season is required")
    ctx.sources.append({"type": "stats", "id": f"IPL {season}"})
    return await _stats_call(ctx, lambda: ctx.stats.season_summary(season))


async def team_season_record(ctx: ToolContext, args: dict) -> dict:
    team = await _stats_call(ctx, lambda: _resolve_team(ctx, _text(args.get("team"), "team")))
    season = _season(args.get("season"))
    if season is None:
        raise ToolError("season is required")
    ctx.sources.append({"type": "stats", "id": f"{team} {season}"})
    return await _stats_call(ctx, lambda: ctx.stats.team_season(team, season))


async def team_titles(ctx: ToolContext, args: dict) -> dict:
    team = await _stats_call(ctx, lambda: _resolve_team(ctx, _text(args.get("team"), "team")))
    ctx.sources.append({"type": "stats", "id": f"{team} titles"})
    return await _stats_call(ctx, lambda: ctx.stats.team_titles(team))


async def milestone(ctx: ToolContext, args: dict) -> dict:
    cat = (args.get("category") or "runs").lower()
    if cat not in ("runs", "wickets", "sixes"):
        raise ToolError("category must be runs, wickets or sixes")
    try:
        threshold = int(args.get("threshold"))
    except (TypeError, ValueError):
        raise ToolError("threshold must be a whole number") from None
    if not 1 <= threshold <= 100_000:
        raise ToolError("threshold must be between 1 and 100000")
    ctx.sources.append({"type": "stats", "id": f"{threshold}+ {cat}"})
    return await _stats_call(ctx, lambda: ctx.stats.milestone(cat, threshold))


async def top_players(ctx: ToolContext, args: dict) -> dict:
    cat = (args.get("category") or "runs").lower()
    if cat not in ("runs", "wickets", "sixes"):
        raise ToolError("category must be runs, wickets or sixes")
    season = _season(args.get("season"))
    limit = int(args.get("limit") or 5)
    ctx.sources.append({"type": "stats", "id": f"top {cat} {season or 'all-time'}"})
    rows = await _stats_call(ctx, lambda: ctx.stats.top(cat, season, limit))
    return {"category": cat, "season": season or "2008-2017", "leaders": rows}


async def search_cricket_knowledge(ctx: ToolContext, args: dict) -> dict:
    query = _text(args.get("query"), "query", 200)
    try:
        hits = await ctx.knowledge.search(query, k=4)
    except KnowledgeUnavailable as e:
        raise ToolError(f"knowledge search unavailable: {e}") from None
    for h in hits:
        ctx.sources.append({"type": "doc", "id": h["id"]})
    return {"results": [h["text"] for h in hits] or ["no relevant notes found"]}


# --- live agent ---------------------------------------------------------------------
async def get_live_score(ctx: ToolContext, args: dict) -> dict:
    """Compact live score: both innings, the chase, who's in and the last event."""
    snap = ctx.snapshot
    ctx.sources.append({"type": "live", "id": snap.as_of()})
    out = {"match": snap.title, "status": snap.status, "as_of": snap.as_of(),
           "innings": [{"team": i.batting_team, "runs": i.runs, "wickets": i.wickets, "overs": i.overs, "run_rate": i.run_rate}
                       for i in snap.innings], "last_event": snap.last_event}
    if snap.status == "live":
        out["at_the_crease"] = {"striker": snap.striker, "non_striker": snap.non_striker, "bowler": snap.current_bowler}
    if c := snap.chase():
        out["chase"] = c
    if snap.result:
        out["result"] = snap.result
    return out


async def get_ball_by_ball(ctx: ToolContext, args: dict) -> dict:
    """The most recent deliveries, newest first, as written on the commentary feed."""
    try:
        n = int(args.get("last_n") or 6)
    except (TypeError, ValueError):
        raise ToolError("last_n must be a whole number") from None
    n = max(1, min(n, 24))
    snap = ctx.snapshot
    ctx.sources.append({"type": "live", "id": snap.as_of()})
    balls = [{"over": c.get("over"), "text": c.get("text")} for c in snap.commentary if c.get("kind", "ball") == "ball"][:n]
    return {"as_of": snap.as_of(), "last_balls_compact": snap.last_balls[-n:], "deliveries": balls or "no deliveries yet"}


async def win_probability(ctx: ToolContext, args: dict) -> dict:
    """A transparent estimate for the side chasing, from runs needed, balls left and wickets in hand."""
    snap = ctx.snapshot
    c, inn = snap.chase(), snap.current
    ctx.sources.append({"type": "live", "id": snap.as_of()})
    if not c or not inn:
        return {"available": False, "reason": "only during the second innings (the chase)"}
    if c["runs_needed"] <= 0:
        p = 100.0
    elif c["balls_left"] <= 0 or inn.wickets >= 10:
        p = 0.0
    else:
        import math
        # Required rate against a par of ~8.3/over, scaled by resources left. A simple model, not a forecast.
        rrr = 6 * c["runs_needed"] / c["balls_left"]
        wkts_left = 10 - inn.wickets
        score = 1.1 * (8.3 - rrr) + 0.35 * (wkts_left - 5) + 0.01 * c["balls_left"] - 0.3
        p = round(100 / (1 + math.exp(-score)), 1)
    return {"chasing_team": inn.batting_team, "chasing_team_win_pct": p, "as_of": snap.as_of(), **c,
            "method": "simple estimate from required run rate, wickets in hand and balls left; not a betting model"}


# --- stats analyst --------------------------------------------------------------------
async def venue_stats(ctx: ToolContext, args: dict) -> dict:
    name = _text(args.get("venue"), "venue", 80)
    venue = await _stats_call(ctx, lambda: ctx.stats.resolve_venue(name))
    if not venue:
        raise ToolError(f"no IPL venue found matching '{name}'")
    ctx.sources.append({"type": "stats", "id": f"venue {venue}"})
    return await _stats_call(ctx, lambda: ctx.stats.venue_stats(venue))


async def toss_impact(ctx: ToolContext, args: dict) -> dict:
    season = _season(args.get("season"))
    team = None
    if args.get("team"):
        team = await _stats_call(ctx, lambda: _resolve_team(ctx, _text(args.get("team"), "team")))
    ctx.sources.append({"type": "stats", "id": f"toss {team or 'all'} {season or 'all'}"})
    return await _stats_call(ctx, lambda: ctx.stats.toss_impact(season, team))


async def batter_vs_bowler(ctx: ToolContext, args: dict) -> dict:
    batter = await _stats_call(ctx, lambda: _resolve_player(ctx, _text(args.get("batter"), "batter")))
    bowler = await _stats_call(ctx, lambda: _resolve_player(ctx, _text(args.get("bowler"), "bowler")))
    ctx.sources.append({"type": "stats", "id": f"{batter} v {bowler}"})
    return await _stats_call(ctx, lambda: ctx.stats.batter_vs_bowler(batter, bowler))


async def dismissal_breakdown(ctx: ToolContext, args: dict) -> dict:
    player = await _stats_call(ctx, lambda: _resolve_player(ctx, _text(args.get("player"), "player")))
    ctx.sources.append({"type": "stats", "id": f"{player} dismissals"})
    return await _stats_call(ctx, lambda: ctx.stats.dismissals(player))


async def find_matches(ctx: ToolContext, args: dict) -> dict:
    a = await _stats_call(ctx, lambda: _resolve_team(ctx, args["team_a"])) if args.get("team_a") else None
    b = await _stats_call(ctx, lambda: _resolve_team(ctx, args["team_b"])) if args.get("team_b") else None
    season = _season(args.get("season"))
    if not (a or b or season):
        raise ToolError("give at least a team or a season")
    ctx.sources.append({"type": "stats", "id": f"matches {a or ''} {b or ''} {season or ''}".strip()})
    return await _stats_call(ctx, lambda: ctx.stats.find_matches(a, b, season))


async def player_of_match_awards(ctx: ToolContext, args: dict) -> dict:
    season = _season(args.get("season"))
    player = None
    if args.get("player"):
        player = await _stats_call(ctx, lambda: _resolve_player(ctx, _text(args.get("player"), "player")))
    ctx.sources.append({"type": "stats", "id": f"player of the match {player or season or 'all'}"})
    return await _stats_call(ctx, lambda: ctx.stats.player_of_match(player, season))


async def team_profile(ctx: ToolContext, args: dict) -> dict:
    team = await _stats_call(ctx, lambda: _resolve_team(ctx, _text(args.get("team"), "team")))
    ctx.sources.append({"type": "stats", "id": f"{team} profile"})
    return await _stats_call(ctx, lambda: ctx.stats.team_profile(team))


async def phase_stats(ctx: ToolContext, args: dict) -> dict:
    player = await _stats_call(ctx, lambda: _resolve_player(ctx, _text(args.get("player"), "player")))
    phase = (args.get("phase") or "death").lower()
    if phase not in ("powerplay", "middle", "death"):
        raise ToolError("phase must be powerplay, middle or death")
    role = "bowling" if str(args.get("role") or "batting").lower().startswith("bowl") else "batting"
    ctx.sources.append({"type": "stats", "id": f"{player} {role} {phase}"})
    return await _stats_call(ctx, lambda: ctx.stats.phase_stats(player, phase, role))


# --- rules expert ---------------------------------------------------------------------
async def search_rules(ctx: ToolContext, args: dict) -> dict:
    query = _text(args.get("query"), "query", 200)
    try:
        hits = await ctx.knowledge.search(query, k=3, where={"type": "rule"}, max_distance=0.6)
    except KnowledgeUnavailable as e:
        raise ToolError(f"rule book search unavailable: {e}") from None
    hits = [h for h in hits if h.get("type") == "rule"]
    for h in hits:
        ctx.sources.append({"type": "doc", "id": h["id"]})
        ctx.evidence.append({"source": f"doc:{h['id']}", "type": "rule", "text": h["text"]})
    return {"rules": [{"id": h["id"], "text": h["text"]} for h in hits] or "no matching rule in the rule book"}


# --- mock tools: no data source is connected, so these return simulated values -----
MOCK_NOTE = "MOCK DATA: simulated, no live provider is connected. Say it is simulated if you use it."


def _seeded(name: str) -> int:
    import hashlib
    return int(hashlib.sha256(name.lower().encode()).hexdigest()[:8], 16)


async def get_weather(ctx: ToolContext, args: dict) -> dict:
    place = _text(args.get("venue") or ctx.snapshot.venue, "venue", 80)
    h = _seeded(place)
    return {"mock": True, "note": MOCK_NOTE, "venue": place, "temperature_c": 26 + h % 9, "humidity_pct": 45 + h % 40,
            "rain_chance_pct": (h >> 4) % 35, "dew_expected_later": bool((h >> 8) % 2)}


async def get_pitch_report(ctx: ToolContext, args: dict) -> dict:
    place = _text(args.get("venue") or ctx.snapshot.venue, "venue", 80)
    h = _seeded(place)
    surface = ["flat, good for batting", "dry, will help spinners later", "green, some early seam movement", "two-paced, slows down"]
    return {"mock": True, "note": MOCK_NOTE, "venue": place, "surface": surface[h % 4], "par_score_estimate": 150 + (h >> 3) % 40}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Awaitable[dict]]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


SEASON = {"type": "integer", "description": "IPL season year, 2008-2017. Omit for career totals."}
TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_live_match", "Full live scorecard of the match being played now.", _obj({}, []), get_live_match),
    Tool("get_player_stats", "IPL batting or bowling statistics for a player, career or one season.",
         _obj({"player": {"type": "string", "description": "Player name, e.g. 'MS Dhoni' or 'Kohli'"},
               "role": {"type": "string", "enum": ["batting", "bowling", "both"]},
               "season": SEASON}, ["player"]), get_player_stats),
    Tool("head_to_head", "Win/loss record between two IPL teams, 2008-2017.",
         _obj({"team_a": {"type": "string"}, "team_b": {"type": "string"}}, ["team_a", "team_b"]), head_to_head),
    Tool("season_summary", "Champion, runner-up, final and top run scorers / wicket takers of one IPL season.",
         _obj({"season": SEASON}, ["season"]), season_summary),
    Tool("team_season_record", "Matches played and won by a team in one IPL season.",
         _obj({"team": {"type": "string"}, "season": SEASON}, ["team", "season"]), team_season_record),
    Tool("team_titles", "IPL titles a team won (seasons) and finals it lost, 2008-2017.",
         _obj({"team": {"type": "string"}}, ["team"]), team_titles),
    Tool("milestone", "Players with at least N career IPL runs, wickets or sixes (2008-2017).",
         _obj({"category": {"type": "string", "enum": ["runs", "wickets", "sixes"]}, "threshold": {"type": "integer"}},
              ["category", "threshold"]), milestone),
    Tool("top_players", "Leaderboard of most runs, wickets or sixes, for one season or all seasons.",
         _obj({"category": {"type": "string", "enum": ["runs", "wickets", "sixes"]}, "season": SEASON,
               "limit": {"type": "integer"}}, ["category"]), top_players),
    Tool("search_cricket_knowledge", "Search cricket rules (DRS, LBW, free hit, DLS, powerplay) and IPL match, team and player notes.",
         _obj({"query": {"type": "string"}}, ["query"]), search_cricket_knowledge),
    # live agent
    Tool("get_live_score", "Compact live score: both innings, the chase, batters at the crease, last event.", _obj({}, []), get_live_score),
    Tool("get_ball_by_ball", "The last N deliveries of the live match, newest first, from the commentary feed.",
         _obj({"last_n": {"type": "integer", "description": "How many deliveries, 1-24 (default 6)"}}, []), get_ball_by_ball),
    Tool("win_probability", "Estimated win chance of the chasing team in the live match (simple model, second innings only).",
         _obj({}, []), win_probability),
    Tool("get_weather", "MOCK: simulated weather at a venue (no weather provider is connected).",
         _obj({"venue": {"type": "string"}}, []), get_weather),
    Tool("get_pitch_report", "MOCK: simulated pitch report for a venue (no pitch data is available).",
         _obj({"venue": {"type": "string"}}, []), get_pitch_report),
    # stats analyst
    Tool("query_player_stats", "IPL batting or bowling statistics for a player, career or one season (2008-2017).",
         _obj({"player": {"type": "string", "description": "Player name, e.g. 'MS Dhoni' or 'Kohli'"},
               "role": {"type": "string", "enum": ["batting", "bowling", "both"]},
               "season": SEASON}, ["player"]), get_player_stats),
    Tool("phase_stats", "A player's batting or bowling record in one phase: powerplay (overs 1-6), middle (7-15) or death (16-20).",
         _obj({"player": {"type": "string"}, "phase": {"type": "string", "enum": ["powerplay", "middle", "death"]},
               "role": {"type": "string", "enum": ["batting", "bowling"]}}, ["player", "phase"]), phase_stats),
    Tool("batter_vs_bowler", "Head-to-head between one batter and one bowler: balls, runs, dismissals, strike rate.",
         _obj({"batter": {"type": "string"}, "bowler": {"type": "string"}}, ["batter", "bowler"]), batter_vs_bowler),
    Tool("dismissal_breakdown", "How a player got out (caught, bowled, lbw...), who dismissed them most, and how they took wickets.",
         _obj({"player": {"type": "string"}}, ["player"]), dismissal_breakdown),
    Tool("venue_stats", "Record at an IPL venue or city: matches, wins batting first vs chasing, average first-innings score, top scorers.",
         _obj({"venue": {"type": "string", "description": "Stadium or city, e.g. 'Wankhede' or 'Bangalore'"}}, ["venue"]), venue_stats),
    Tool("team_profile", "A team's whole IPL story 2008-2017: matches, wins, titles, finals lost, best season, top run scorers.",
         _obj({"team": {"type": "string"}}, ["team"]), team_profile),
    Tool("toss_impact", "How often the toss winner won the match, overall or by decision (bat/field), optionally for a season or team.",
         _obj({"season": SEASON, "team": {"type": "string"}}, []), toss_impact),
    Tool("find_matches", "List IPL matches by team(s) and/or season, with winners, margins and player of the match.",
         _obj({"team_a": {"type": "string"}, "team_b": {"type": "string"}, "season": SEASON}, []), find_matches),
    Tool("player_of_match_awards", "Player-of-the-match award counts for a player, or the leaders for a season / all seasons.",
         _obj({"player": {"type": "string"}, "season": SEASON}, []), player_of_match_awards),
    # rules expert
    Tool("search_rules", "Search the cricket rule book (no ball, free hit, LBW, DRS, wides, powerplay, super over, DLS...).",
         _obj({"query": {"type": "string"}}, ["query"]), search_rules),
]}


def specs(available: set[str]) -> list[dict]:
    return [t.spec() for name, t in TOOLS.items() if name in available]


def _collect_numbers(obj: Any, out: set[int]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(int(obj))
        if isinstance(obj, float):
            out.add(round(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, str):
        import re
        out.update(int(n) for n in re.findall(r"\d+", obj))


async def run_tool(ctx: ToolContext, name: str, args: Any) -> tuple[str, bool]:
    """Returns (result_json, ok). Errors are returned to the model as text so it can recover or say so."""
    tool = TOOLS.get(name)
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if isinstance(args, str) else {}
        except json.JSONDecodeError:
            args = {}
    t0 = time.monotonic()
    with tracer().start_as_current_span(f"tool.{name}") as span:
        span.set_attribute("tool.args", json.dumps(args)[:300])
        if tool is None:
            TOOL_CALLS.labels("unknown", "error").inc()
            observer.note_tool(name, args, False, 0.0, "unknown tool")
            return json.dumps({"error": f"unknown tool {name}"}), False
        try:
            result = await asyncio.wait_for(tool.handler(ctx, args), timeout=20)
            _collect_numbers(result, ctx.numbers)
            TOOL_CALLS.labels(name, "ok").inc()
            ok = True
        except ToolError as e:
            result, ok = {"error": str(e)}, False
            TOOL_CALLS.labels(name, "error").inc()
        except TimeoutError:
            result, ok = {"error": "tool timed out"}, False
            TOOL_CALLS.labels(name, "timeout").inc()
        span.set_attribute("tool.ok", ok)
        span.set_attribute("tool.ms", round((time.monotonic() - t0) * 1000))
    observer.note_tool(name, args, ok, (time.monotonic() - t0) * 1000, "" if ok else str(result.get("error", "")))
    text = json.dumps(result, default=str)
    if ok:
        ctx.evidence.append({"source": f"tool:{name}", "args": args, "text": text[:MAX_RESULT_CHARS]})
    return (text[:MAX_RESULT_CHARS] + "…" if len(text) > MAX_RESULT_CHARS else text), ok
