"""Router: which specialists does a question need, live, stats or rules (one or more)?

Rules decide first (instant and predictable). Only when no rule fires does the Router agent, a small LLM call,
classify the question. The method used is recorded with every route, so the transcript shows why.
"""

import json
import re
from dataclasses import dataclass, field

from strands import Agent

ROUTES = ("live", "stats", "rules")

LIVE = re.compile(r"\b(score|now|live|this match|today|tonight|current(ly)?|right now|"
                  r"last\b.{0,12}\b(overs?|balls?|deliver(y|ies)|six|four|wicket)|recent balls|ball by ball|"
                  r"required (run )?rate|chase (this|it|down)|the chase|need(s)?\b.*\bfrom|target|"
                  r"win(ning)? (chance|probability)|who will win|can they (still )?(win|chase|do it)|still win|"
                  r"at the crease|on strike|weather|rain|dew|pitch|surface|toss in this)\b", re.I)
RULES = re.compile(r"\b(rules?|laws?|no[- ]?balls?|free[- ]hit|lbw|leg before|drs|reviews?|umpire'?s call|wides?|byes?|leg[- ]byes?|"
                   r"fielding restrictions?|super over|dls|duckworth|run[- ]out|stumped|stumping|obstruct\w*|retired|"
                   r"is it legal|allowed to|what happens (if|when|after)|how many overs can)\b", re.I)
STATS = re.compile(r"\b(runs?|wickets?|season|career|record|average|strike rate|economy|titles?|champions?|final|"
                   r"vs|versus|against|head to head|best|most|top|compare|venue|stadium|toss|dismiss\w*|awards?|20(0[89]|1[0-7]))\b", re.I)

# Phrases that are only about the match being played now. Unlike LIVE, no loose words ("score", "pitch") that history
# questions use too ("Kohli's highest score"), so a question with one of these always needs the Live desk.
LIVE_NOW = re.compile(r"\b(right now|this match|today|tonight|currently|required (run )?rate|asking rate|the chase|"
                      r"chase (this|it|down)|still win|can they (still )?(win|chase|do it)|win(ning)? (chance|probability)|"
                      r"who will win|at the crease|on strike|last (few |\d+ )?(overs|balls))\b", re.I)
# The question itself asks for history, even when it is also about the live match.
HISTORY = re.compile(r"\b(head[- ]to[- ]head|h2h|in (ipl )?history|all[- ]time|rivalry|previous(ly)? (meetings|seasons)|"
                     r"(19|20)\d{2})\b", re.I)

ROUTER_PROMPT ="""You route questions for a cricket chatbot. Pick every desk the question needs:
- live: the match being played right now (score, chase, recent balls, who is batting, conditions)
- stats: IPL history 2008-2017 (players, teams, seasons, records, match-ups, venues)
- rules: laws of cricket (no ball, LBW, DRS, wides, powerplay, super over, DLS)
Reply with JSON only, for example {"routes": ["stats"], "reason": "asks for a player's career record"}."""


@dataclass
class Route:
    routes: list[str]
    method: str                 # "rules" | "llm" | "default"
    reason: str = ""
    matched: dict = field(default_factory=dict)

    def event(self) -> dict:
        return {"type": "route", "routes": self.routes, "method": self.method, "reason": self.reason}


def route_by_rules(q: str, *, live_q: bool, has_stats_plan: bool, entities: bool, rule_hit: bool,
                   own_entities: bool = True, own_analysis: bool = False) -> Route | None:
    """own_entities: the question names players/teams itself. A live follow-up ("can they still win this?")
    must not inherit the stats desk just because the previous question named players.
    own_analysis: the question itself maps to a history lookup (a venue, the toss...), so it is not about
    the live match even if a loose live word like "chase" appears."""
    routes, why = [], []
    live = bool(LIVE.search(q)) or (live_q and not own_analysis)
    if live:
        routes.append("live")
        why.append("asks about the live match")
    if live and not own_entities and not own_analysis and not (has_stats_plan and HISTORY.search(q)):
        pass  # a live follow-up doesn't inherit the stats desk; "...and their head-to-head?" asks for it itself
    elif has_stats_plan or (entities and STATS.search(q)) or (entities and not RULES.search(q)):
        routes.append("stats")
        why.append("names players/teams or asks for records")
    # A retrieved rule document only adds the Rules expert when nothing else fits: "is the required rate rising?"
    # pulls up a rule-book chunk about run rates, but it's a live question, not a law of the game.
    if RULES.search(q) or (rule_hit and not routes):
        routes.append("rules")
        why.append("asks about a law of the game")
    return Route(routes, "rules", "; ".join(why)) if routes else None


async def route_by_llm(model, q: str) -> Route:
    """The Router agent: one short LLM call, for questions no rule recognises."""
    router = Agent(model=model, system_prompt=ROUTER_PROMPT, callback_handler=None, name="Router", tools=[])
    res = await router.invoke_async(f"Question: {q}")
    text = str(res)
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        routes = [r for r in data.get("routes", []) if r in ROUTES]
        if routes:
            return Route(routes, "llm", str(data.get("reason", ""))[:160])
    except (json.JSONDecodeError, AttributeError, ValueError):
        pass
    return Route(["stats"], "default", "router reply unusable; defaulting to the stats desk")
