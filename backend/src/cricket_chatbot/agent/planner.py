"""Deterministic tool planner.

Small local models answer far more accurately when the right data is already in the prompt than when they have
to decide which tool to call. The planner spots players, teams, seasons and leaderboard questions and runs those
tools up front. The model can still call more tools itself.
"""

import re

from ..data.stats_db import PLAYER_ALIASES, TEAM_ALIASES, StatsDB

SAME_SEASON = re.compile(r"\b(that|same|this) (season|year)\b", re.I)
YEAR = re.compile(r"\b(20(?:0[89]|1[0-7]))\b")
SEASON_WORDS = re.compile(r"\b(won|win|winner|winners|champion|champions|title|final|orange|purple|top|most|best|season|cap|"
                          r"kisne|jeet\w*|jita|kaun)\b|जीत|जीता|چیمپئن|جیت", re.I)
LEADER = re.compile(r"\b(most|top|highest|leading|best)\b.*\b(runs?|wickets?|sixes|6s|run[- ]scorers?|wicket[- ]takers?)\b", re.I)
BOWL_WORDS = re.compile(r"\b(bowl|bowling|bowler|wickets?|economy)\b|विकेट|गेंदबाज", re.I)
BAT_WORDS = re.compile(r"\b(bat|batting|batter|batsman|runs|strike rate|average|sixes|fours|century|fifty)\b|रन|बल्लेबाज", re.I)
MILESTONE = re.compile(r"(?<![\d.])(\d{2,6})\s*\+?\s*(?:ipl\s+|career\s+)?(runs|wickets|sixes)\b", re.I)
STOP = {"who", "what", "when", "where", "which", "how", "why", "ipl", "the", "and", "for", "did", "does", "has", "have",
        "is", "was", "are", "in", "of", "vs", "versus", "match", "season", "stats", "career", "score", "runs", "final",
        "team", "best", "most", "top", "india", "pakistan", "today", "now", "tell", "about", "give", "show", "can"}


def _surname_index(db: StatsDB) -> dict[str, str]:
    idx: dict[str, str] = {}
    for p in db.players:  # most active first, so the first claim on a surname wins
        last = p.split()[-1].lower()
        if len(last) >= 4 and last not in STOP:
            idx.setdefault(last, p)
    return idx


class Planner:
    def __init__(self, db: StatsDB):
        self.db = db
        self._surnames: dict[str, str] | None = None
        self._full = {p.lower(): p for p in db.players} if db else {}

    @property
    def surnames(self) -> dict[str, str]:
        if self._surnames is None:
            self._surnames = _surname_index(self.db)
        return self._surnames

    def players(self, q: str, live_players: tuple[str, ...] = ()) -> list[str]:
        found: list[str] = []
        live_surnames = {p.split()[-1].lower(): p for p in live_players}
        low = q.lower()
        for alias, name in PLAYER_ALIASES.items():
            hit = alias in q if not alias.isascii() else re.search(rf"\b{re.escape(alias)}\b", low)
            if hit and name not in found:
                found.append(name)
        for full_lower, name in self._full.items():
            if len(full_lower) > 6 and " " in full_lower and name not in found \
                    and re.search(rf"(?<![\w]){re.escape(full_lower)}\b", low):  # whole words: "virat kohli" must not match "t kohli"
                found.append(name)
        for word in re.findall(r"\b[A-Z][a-zA-Z']{3,}\b", q):  # capitalised words only, to skip plain English
            name = live_surnames.get(word.lower()) or self.surnames.get(word.lower())  # this match's players first
            if name and name not in found and word.lower() not in STOP:
                found.append(name)
        return found[:2]

    def teams(self, q: str) -> list[str]:
        found: list[str] = []
        low = q.lower()
        for t in self.db.teams:
            if t.lower() in low and t not in found:
                found.append(t)
        for alias, team in TEAM_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", low) and team not in found:
                found.append(team)
        return found[:2]

    def plan(self, q: str, live_teams: tuple[str, str] | None = None,
             live_players: tuple[str, ...] = (), context_q: str = "") -> list[tuple[str, dict]]:
        """context_q is the previous question when q is a follow-up ("and his career runs?"): it supplies the
        player or team the follow-up refers to, but never its season unless q says "that season"."""
        calls: list[tuple[str, dict]] = []
        years = YEAR.findall(q)
        if not years and context_q and SAME_SEASON.search(q):
            years = YEAR.findall(context_q)
        season = int(years[0]) if years else None
        players = self.players(q, live_players) or (self.players(context_q, live_players) if context_q else [])
        teams = self.teams(q) or (self.teams(context_q) if context_q else [])
        if context_q and not players and not teams and not years:
            q = f"{q} {context_q}"  # nothing of its own: plan on the previous question's subject

        if (m := MILESTONE.search(q)) and not players and re.search(r"\b(who|which|players?|how many|list|anyone)\b", q, re.I):
            calls.append(("milestone", {"category": m.group(2).lower(), "threshold": int(m.group(1))}))
            return calls
        # "2016 compared to 2015": one lookup per season, or the answer compares against a year it never saw.
        player_seasons = sorted({int(y) for y in years})[:3] or [None]
        for p in players:
            role = "bowling" if BOWL_WORDS.search(q) and not BAT_WORDS.search(q) else (
                "batting" if BAT_WORDS.search(q) and not BOWL_WORDS.search(q) else self.main_role(p, allow_both=True))
            for y in player_seasons:
                calls.append(("get_player_stats", {"player": p, "role": role, **({"season": y} if y else {})}))
        if LEADER.search(q) and not players:
            cat = "wickets" if re.search(r"wicket", q, re.I) else ("sixes" if re.search(r"six|6s", q, re.I) else "runs")
            calls.append(("top_players", {"category": cat, **({"season": season} if season else {})}))
        if season and SEASON_WORDS.search(q) and not players:
            for y in sorted({int(y) for y in YEAR.findall(q)})[:4]:   # "2008 2009 2010 winners" -> one lookup each
                calls.append(("season_summary", {"season": y}))
        if teams and re.search(r"\b(ever win|ever won|titles?|trophy|trophies|champions?|how many times)\b", q, re.I) and not season:
            for t in teams:
                calls.append(("team_titles", {"team": t}))
        elif len(teams) == 2 and re.search(r"\b(vs|versus|against|head to head|h2h|record|rivalry|between)\b", q, re.I):
            calls.append(("head_to_head", {"team_a": teams[0], "team_b": teams[1]}))
        elif len(teams) == 1 and season:
            for y in sorted({int(y) for y in years})[:4]:   # "2010 and 2011" -> one lookup each
                calls.append(("team_season_record", {"team": teams[0], "season": y}))
        elif re.search(r"\b(head to head|h2h|rivalry|these (two )?teams)\b", q, re.I) and live_teams:
            calls.append(("head_to_head", {"team_a": live_teams[0], "team_b": live_teams[1]}))
        return calls[:4]

    # --- deeper questions for the multi-agent system --------------------------------------
    PHASE = re.compile(r"\b(death|slog|last (?:few|four|five) overs|powerplay|power ?play|first six overs|middle overs)\b", re.I)
    VERSUS = re.compile(r"\b(vs\.?|versus|against|face[sd]?|facing)\b", re.I)
    DISMISSAL = re.compile(r"\b(get|gets|got|getting) out\b|\bdismiss(?:ed|al|als)?\b|\bweakness(?:es)?\b|\bout most\b", re.I)
    TOSS = re.compile(r"\btoss\b", re.I)
    VENUE = re.compile(r"\b(venue|stadium|ground|chinnaswamy|wankhede|eden gardens|kotla|chepauk|chidambaram|uppal|mohali|"
                       r"sawai mansingh|bangalore|bengaluru|mumbai|kolkata|delhi|chennai|hyderabad|jaipur|pune|chandigarh)\b", re.I)
    POTM = re.compile(r"\b(player|man) of the match\b|\bpotm\b|\bmotm\b", re.I)
    MATCH_LIST = re.compile(r"\b(list|which|all)\b.*\b(matches|games)\b|\bresults?\b.*\b(matches|games)\b", re.I)

    def main_role(self, player: str, allow_both: bool = False) -> str:
        """'bowling' if the player bowled more balls than they faced, else 'batting'. With allow_both, a genuine
        all-rounder (minor role at least 40% of the major one) gets 'both'."""
        r = self.db.q("""SELECT SUM(CASE WHEN batsman = ? THEN 1 ELSE 0 END) AS faced,
                SUM(CASE WHEN bowler = ? THEN 1 ELSE 0 END) AS bowled FROM deliveries WHERE batsman = ? OR bowler = ?""",
                      (player, player, player, player))[0]
        faced, bowled = r["faced"] or 0, r["bowled"] or 0
        if allow_both and min(faced, bowled) >= 0.4 * max(faced, bowled, 1):
            return "both"
        return "bowling" if bowled > faced else "batting"

    def analysis_plan(self, q: str, live_players: tuple[str, ...] = ()) -> list[tuple[str, dict]]:
        """Lookups the older planner can't express: phases, match-ups, dismissals, venues, toss, awards, match lists.
        A non-empty plan means the question needs the Stats analyst, not the one-line fact composer."""
        calls: list[tuple[str, dict]] = []
        low = q.lower()
        # In the order the question mentions them: "Kohli against Bumrah" is Kohli batting to Bumrah.
        players = sorted(self.players(q, live_players),
                         key=lambda p: min((low.find(w.lower()) for w in p.split() if len(w) > 2 and w.lower() in low), default=999))
        teams = self.teams(q)
        years = YEAR.findall(q)
        season = int(years[0]) if years else None
        if players and (m := self.PHASE.search(q)):
            w = m.group(1).lower()
            phase = "powerplay" if ("power" in w or "first six" in w) else ("middle" if "middle" in w else "death")
            for p in players[:2]:
                if BAT_WORDS.search(q) and not BOWL_WORDS.search(q):
                    role = "batting"
                elif BOWL_WORDS.search(q) and not BAT_WORDS.search(q):
                    role = "bowling"
                else:
                    role = self.main_role(p)
                calls.append(("phase_stats", {"player": p, "phase": phase, "role": role}))
        if len(players) >= 2 and self.VERSUS.search(q):
            bat, bowl = players[0], players[1]
            if self.main_role(bat) == "bowling" and self.main_role(bowl) == "batting":  # "Bumrah vs Kohli"
                bat, bowl = bowl, bat
            calls.append(("batter_vs_bowler", {"batter": bat, "bowler": bowl}))
        if players and self.DISMISSAL.search(q):
            calls.append(("dismissal_breakdown", {"player": players[0]}))
        if self.TOSS.search(q) and not re.search(r"\b(this|the) match\b|\btoday\b|\bwho won the toss\b", q, re.I):
            calls.append(("toss_impact", {**({"season": season} if season else {}), **({"team": teams[0]} if len(teams) == 1 else {})}))
        if (m := self.VENUE.search(q)) and not players and not self.TOSS.search(q) and \
                re.search(r"\b(at|in|venue|stadium|ground|record|score|chase|chasing|bat first)\b", q, re.I):
            name = m.group(1)
            if not (len(teams) and name.lower() in " ".join(teams).lower()):  # "Mumbai" inside "Mumbai Indians" is a team
                calls.append(("venue_stats", {"venue": name}))
        if self.POTM.search(q) and not re.search(r"\bfinal\b", q, re.I):
            calls.append(("player_of_match_awards", {**({"player": players[0]} if players else {}), **({"season": season} if season else {})}))
        if len(teams) == 1 and not players and not season and \
                re.search(r"\b(history|overall|all[- ]time|tell me about|how good|story|record)\b", q, re.I):
            calls.append(("team_profile", {"team": teams[0]}))
        if self.MATCH_LIST.search(q) and (teams or season) and not players:
            calls.append(("find_matches", {**({"team_a": teams[0]} if teams else {}), **({"team_b": teams[1]} if len(teams) > 1 else {}),
                                           **({"season": season} if season else {})}))
        return calls[:4]
