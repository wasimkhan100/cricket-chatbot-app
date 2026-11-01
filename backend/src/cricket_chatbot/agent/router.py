"""Intent routing. Short factual questions about the live match are answered from the state, with no LLM call.

Anything that asks for explanation, comparison, prediction or history goes to the agent.
"""

import re

from ..live.state import MatchState

NEEDS_LLM = re.compile(r"\b(why|how come|how is|how does|how do|how are|rain|revis\w*|calculat\w*|method|dls|duckworth|explain|analy[sz]e|compare|predict|should|would|could|if|chance|think|"
                       r"better|best|worst|history|career|season|ever|record|rule|mean|vs|versus|against|ipl)\b|\b(19|20)\d{2}\b", re.I)
INTENTS: list[tuple[str, re.Pattern]] = [
    ("greeting", re.compile(r"^(hi|hello|hey|namaste|salaam|yo)\b[!. ]*$", re.I)),
    ("toss", re.compile(r"\btoss\b", re.I)),
    ("result", re.compile(r"\b(who won|who is winning|who'?s winning|result|final score)\b", re.I)),
    ("chase", re.compile(r"\b(target|need|needs|required|rrr|required run rate|how many (runs )?to win|equation)\b", re.I)),
    ("batting_now", re.compile(r"\b(who'?s|who is|who are)\s+(batting|at the crease|on strike|in)\b", re.I)),
    ("bowling_now", re.compile(r"\b(who'?s|who is)\s+bowling\b", re.I)),
    ("last_ball", re.compile(r"\b(last ball|what (just )?happened|latest|last over)\b", re.I)),
    ("score", re.compile(r"\b(score|scorecard|scores|how many runs|total|wickets down|kitna hua|kya score)\b", re.I)),
]
LIVE_HINT = re.compile(r"\b(now|current|currently|live|today|this match|score|batting|bowling|over|overs|need|target|"
                       r"chase|strike|striker|partnership|last ball|required|innings|wicket|wickets|run rate)\b", re.I)


def classify(question: str) -> str | None:
    q = question.strip()
    if len(q.split()) > 12 or NEEDS_LLM.search(q):
        return None
    for name, pattern in INTENTS:
        if pattern.search(q):
            return name
    return None


def is_live_question(question: str) -> bool:
    return bool(LIVE_HINT.search(question))


def _inn_line(s: MatchState, i: int) -> str:
    inn = s.innings[i]
    return f"{inn.batting_team} {inn.runs}/{inn.wickets} ({inn.overs} ov, RR {inn.run_rate})"


def template_answer(intent: str, s: MatchState) -> str | None:
    if intent == "greeting":
        return (f"Hi! I'm following {s.title} live. Ask me the score, who's batting, the chase, a player's IPL stats, "
                f"or any cricket rule.")
    if intent == "toss":
        return f"{s.toss}."
    if not s.innings:
        return f"{s.title} hasn't started yet. {s.toss}."
    inn = s.current
    if intent == "score":
        parts = [_inn_line(s, i) for i in range(len(s.innings))]
        tail = ""
        if c := s.chase():
            tail = f" {inn.batting_team} need {c['runs_needed']} from {c['balls_left']} balls (RRR {c['required_run_rate']})."
        if s.status == "complete":
            tail = f" {s.result}"
        return " · ".join(parts) + "." + tail
    if intent == "chase":
        if s.status == "complete":
            return s.result
        c = s.chase()
        if not c:
            return (f"{inn.batting_team} are batting first: {inn.runs}/{inn.wickets} after {inn.overs} overs, "
                    f"run rate {inn.run_rate}. There's no target yet.")
        return (f"{inn.batting_team} need {c['runs_needed']} runs from {c['balls_left']} balls to beat a target of "
                f"{c['target']}. Required run rate {c['required_run_rate']}, current run rate {inn.run_rate}.")
    if intent == "batting_now":
        if s.status == "complete":
            return f"The match is over. {s.result}"
        st = inn.batters.get(s.striker)
        ns = inn.batters.get(s.non_striker)
        fmt = lambda b: f"{b.name} {b.runs}* ({b.balls})" if b else "?"
        return f"{fmt(st)} is on strike, with {fmt(ns)} at the other end."
    if intent == "bowling_now":
        b = inn.bowlers.get(s.current_bowler)
        if not b:
            return "No bowler yet."
        return f"{b.name} is bowling: {b.overs} overs, {b.runs} runs, {b.wickets} wicket{'s' if b.wickets != 1 else ''}."
    if intent == "last_ball":
        latest = s.commentary[0]["text"] if s.commentary else s.last_event
        return f"{latest} Score: {_inn_line(s, len(s.innings) - 1)}."
    if intent == "result":
        if s.status == "complete":
            return s.result
        c = s.chase()
        if c:
            return (f"Still live. {inn.batting_team} need {c['runs_needed']} from {c['balls_left']} balls "
                    f"(RRR {c['required_run_rate']}).")
        return f"Still live, first innings: {_inn_line(s, 0)}."
    return None


def suggestions(s: MatchState) -> list[str]:
    if not s.innings:
        return ["Who won the toss?", "Head to head record of these teams?", "What is a free hit?"]
    out = ["What's the score?"]
    if s.chase():
        out.append("What's the required run rate?")
        out.append("Can they chase this down?")
    else:
        out.append("What's a good total at this venue?")
    if s.striker:
        out.append(f"How has {s.striker} performed in IPL history?")
    if s.current_bowler:
        out.append(f"Career bowling stats of {s.current_bowler}?")
    if s.last_event.startswith("WICKET"):
        out.append("Explain that dismissal")
    out.append("Who won IPL 2016?")
    return out[:6]
