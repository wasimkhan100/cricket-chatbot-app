"""Fast answers without the LLM, plus scope and topic checks.

On a CPU the LLM takes 5-20 s; most fan questions are lookups the database can answer exactly in ~100 ms.
`compose()` writes those answers from tool results, `rule_answer()` returns the curated rule text, and the
scope checks stop the model from guessing about things outside its data.
"""

import re

# Questions that need reasoning or explanation always go to the LLM.
NEEDS_REASONING = re.compile(r"\b(why|how come|explain|compare|comparison|better|best|worst|greatest|should|would|could|"
                             r"predict|chance|think|opinion|analy[sz]e|difference|good|great|form|rate him|rank)\b", re.I)

CRICKET_TERMS = re.compile(
    r"\b(cricket|ipl|t20|match(es)?|innings|overs?|wickets?|runs?|bat(ting|ter|sman|smen)?|bowl(ing|er|ers)?|sixe?s|fours?|"
    r"boundar(y|ies)|toss|chase|target|umpire|drs|lbw|no[- ]?ball|wide|bye|captain|stadium|pitch|powerplay|super over|"
    r"final|season|scores?|scorecard|partnership|field(er|ing)?|catch|caught|stumped|century|fifty|duck|economy|strike rate|"
    r"run rate|required rate|required run rate|asking rate|"
    r"orange cap|purple cap|dismiss(al|ed)?|all[- ]?rounder|spinner|pacer|yorker|bouncer|collapse|finisher|net run rate|"
    r"duckworth|dls|free hit|tied|titles?|troph(y|ies)|champions?|winners?|player of the match|crickbot|rules?|stats?|statistics|"
    r"career|records?|averages?|scored|players?|teams?|squad|kisne|jeet\w*|jita|kaun jeeta)\b"
    # Hindi (Devanagari) and Urdu cricket words, so fans writing in their own script aren't turned away.
    r"|आईपीएल|क्रिकेट|मैच|विकेट|रन|बल्लेबाज|गेंदबाज|कप्तान|जीत|टीम|स्कोर|छक्के|चौके|ओवर|कोहली|धोनी|रोहित|तेंदुलकर"
    r"|آئی پی ایل|کرکٹ|میچ|وکٹ|رنز|ٹیم|سکور|کپتان|جیت|اوور", re.I)

OTHER_CRICKET = re.compile(r"\b(world cup|worldcup|odi|odis|test match|test cricket|tests|t20i|t20 internationals?|asia cup|"
                           r"ashes|champions trophy|big bash|bbl|psl|cpl|wpl|sa20|the hundred|ranji|county|wtc)\b", re.I)
UNITS = r"runs?|wickets?|balls?|matches|sixes|fours|overs|points|deliveries|catches|centuries|fifties|innings|times"
# A 4-digit number reads as a year unless a cricket unit follows it ("2000 runs" is not the year 2000).
YEAR = re.compile(rf"(?<![\d.])((?:19|20)\d{{2}})(?![\d.])(?!\s*(?:{UNITS})\b)", re.I)
# "ipl 99999", "season 0", "2023 ipl": a number used as a season that isn't 2008-2017.
SEASON_NUMBER = re.compile(r"\b(?:ipl|season|edition|year)\s*[-#]?\s*(\d{4,})\b|\b(\d{4,})\s*(?:ipl|season)\b", re.I)

SCOPE_REPLY = ("My records cover the IPL from 2008 to 2017 and the live match, so I can't answer about {what}. "
               "Ask me about IPL players, seasons, finals, head-to-heads or the rules.")
CLARIFY_REPLY = ("Could you ask a full question? For example: \"What's the score?\", \"Who won IPL 2010?\" "
                 "or \"What is a free hit?\"")
OFF_TOPIC_REPLY = "I'm Crickbot, so I only talk cricket: this live match, IPL 2008-2017 stats and the rules of the game."


def out_of_scope(q: str) -> str | None:
    """Cricket questions about data we don't have. Answered honestly instead of letting the model guess."""
    if m := OTHER_CRICKET.search(q):
        return SCOPE_REPLY.format(what=m.group(0))
    years = YEAR.findall(q) + [a or b for a, b in SEASON_NUMBER.findall(q)]
    inside = sorted({int(y) for y in years if 2008 <= int(y) <= 2017})
    outside = [y for y in years if not 2008 <= int(y) <= 2017]
    if outside:
        # Even mixed with a valid season ("2010 and 2023"), part of the answer would be a guess, so say so.
        also = f" I can tell you about {', '.join(map(str, inside))} though." if inside else ""
        shown = outside[0] if len(outside[0]) <= 8 else outside[0][:8] + "…"   # as typed, "0000" not "0"
        return SCOPE_REPLY.format(what=f"season {shown}") + also
    return None


def is_ipl_year_question(q: str) -> bool:
    return any(2008 <= int(y) <= 2017 for y in YEAR.findall(q))


FILLER = re.compile(r"^(and|or|but|so|also|ok|okay|hmm+|what|who|why|how|he|his|him|she|her|they|them|it|this|that|yes|no|"
                    r"hi|hey|pls|please|bro|yaar|the|a|an|is|are)$", re.I)


def needs_clarification(q: str) -> bool:
    """No real content to answer: "and", "his?", "?!", "a". Answering would mean guessing what the fan meant."""
    words = [w for w in re.findall(r"\w+", q) if not FILLER.match(w)]
    return not words or (len(words) == 1 and len(words[0]) < 2)


def off_topic(q: str, has_entities: bool, top_distance: float | None, follow_up: bool) -> bool:
    """Off topic unless the question has cricket words, a known player/team, continues the chat, or sits very
    close to the cricket knowledge base. Distance alone can't separate them ("next election in India" 0.30 vs
    "why did Mumbai collapse" 0.41), so the vocabulary does most of the work."""
    if follow_up or has_entities or CRICKET_TERMS.search(q) or is_ipl_year_question(q):
        return False
    return top_distance is None or top_distance > 0.25


def _margin(f: dict) -> str:
    return f.get("margin") or ""


def compose(q: str, planned: list[tuple[str, dict, dict]]) -> str | None:
    """Writes a direct answer from planned tool results, or returns None to let the LLM answer."""
    if not planned or NEEDS_REASONING.search(q):
        return None
    # Answer from the most specific lookup: "SRH wins in 2016" is a team record, not the season summary.
    priority = ["milestone", "team_titles", "team_season_record", "head_to_head", "get_player_stats", "top_players", "season_summary"]
    name, args, r = min(planned, key=lambda p: priority.index(p[0]) if p[0] in priority else len(priority))
    planned = [p for p in planned if p[0] == name]
    low = q.lower()

    if name == "season_summary" and r.get("champion") and len(planned) == 1:
        if re.search(r"\b(most|top|highest|leading)\b.*\bruns?\b|orange", low) and r.get("most_runs"):
            top = r["most_runs"][0]
            return f"{top['player']} scored the most runs in IPL {r['season']}: {top['runs']}."
        if re.search(r"\b(most|top|highest|leading)\b.*\bwickets?\b|purple", low) and r.get("most_wickets"):
            top = r["most_wickets"][0]
            return f"{top['player']} took the most wickets in IPL {r['season']}: {top['wickets']}."
        if re.search(r"player of the match", low):
            return f"{r['final']['player_of_match']} was player of the match in the IPL {r['season']} final."
        f = r["final"]
        return (f"{r['champion']} won IPL {r['season']}, beating {r['runner_up']} by {_margin(f)} in the final "
                f"at {f['venue']} on {f['date']}.")

    if name == "season_summary" and len(planned) > 1 and all(p[2].get("champion") for p in planned):
        return " ".join(f"IPL {p[2]['season']}: {p[2]['champion']} beat {p[2]['runner_up']} in the final." for p in planned)

    if name == "milestone":
        cat, n, players = r["category"], r["threshold"], r["players"]
        if not r["count"]:
            return f"No one reached {n} IPL {cat} between 2008 and 2017."
        names = ", ".join(f"{x['player']} ({x['n']})" for x in players[:10])
        more = f" and {r['count'] - 10} more" if r["count"] > 10 else ""
        return f"{r['count']} players reached {n}+ IPL {cat} in 2008-2017: {names}{more}."

    if name == "top_players" and r.get("leaders"):
        cat = r["category"]
        season = r["season"]
        lead = r["leaders"][0]
        rest = ", ".join(f"{x['player']} {x[cat]}" for x in r["leaders"][1:3])
        when = f"IPL {season}" if season != "2008-2017" else "the IPL (2008-2017)"
        verb = {"runs": "scored the most runs", "wickets": "took the most wickets", "sixes": "hit the most sixes"}[cat]
        return f"{lead['player']} {verb} in {when}: {lead[cat]}." + (f" Next: {rest}." if rest else "")

    if name == "team_titles":
        team, seasons, lost = r["team"], r["title_seasons"], r["finals_lost"]
        lost_txt = f" They lost the final in {', '.join(map(str, lost))}." if lost != "none" else ""
        if seasons == "none":
            return f"No. {team} didn't win the IPL between 2008 and 2017.{lost_txt}"
        n = len(seasons)
        return f"{team} won the IPL {n} time{'s' if n > 1 else ''} in 2008-2017: {', '.join(map(str, seasons))}.{lost_txt}"

    if name == "head_to_head" and r.get("matches"):
        a, b, w = r["team_a"], r["team_b"], r["wins"]
        other = sum(v for k, v in w.items() if k not in (a, b))
        return (f"{a} and {b} played {r['matches']} IPL matches in 2008-2017: {a} won {w.get(a, 0)}, {b} won {w.get(b, 0)}"
                + (f", {other} without a result." if other else "."))

    if name == "team_season_record":
        champ = f" {r['season_champion']} won the title that year." if r.get("season_champion") else ""
        return f"{r['team']} won {r['won']} of {r['played']} matches in IPL {r['season']}.{champ}"

    if name == "get_player_stats" and len(planned) == 1:
        when = f"IPL {r['season']}" if isinstance(r.get("season"), int) else "the IPL (2008-2017)"
        if "runs" in r and "wickets" not in r:
            if re.search(r"\bsix", low):
                return f"{r['player']} hit {r['sixes']} sixes in {when}."
            if re.search(r"\bfours?\b", low):
                return f"{r['player']} hit {r['fours']} fours in {when}."
            if r["balls"] == 0:
                return f"{r['player']} didn't bat in {when}."
            return (f"{r['player']} scored {r['runs']} runs in {when}: {r['matches']} matches, average {r['average'] or '-'}, "
                    f"strike rate {r['strike_rate']}, highest score {r['highest_score']}.")
        if "wickets" in r and "runs" not in r:
            if r["overs"] == "0.0":
                return f"{r['player']} didn't bowl in {when}."
            return (f"{r['player']} took {r['wickets']} wickets in {when}: {r['overs']} overs, economy {r['economy']}, "
                    f"average {r['average'] or '-'}.")
    return None


RULE_QUESTION = re.compile(r"^\s*(what('s| is| are| does| happens)|how (does|do|is|are|many)|when (is|does)|define|meaning of|"
                           r"is a|can a|can the)\b", re.I)


def rule_answer(q: str, hits: list[dict]) -> tuple[str, str] | None:
    """For a plain 'what is X' rule question, the curated rule text is the best answer the bot can give."""
    if not hits or not RULE_QUESTION.search(q) or NEEDS_REASONING.search(q):
        return None
    top = hits[0]
    if top.get("type") != "rule" or top["distance"] > 0.32 or top["id"] == "rule-about-this-chatbot-s-data":
        return None
    body = top["text"].split(". ", 1)[1] if ". " in top["text"] else top["text"]
    sentences = re.split(r"(?<=[.!?])\s+", body)
    return " ".join(sentences[:3]), top["id"]
