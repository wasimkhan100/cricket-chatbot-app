"""Turns the raw CSVs and the rules file into retrievable text documents.

Document types: match (one per match), season, team, player (most active players), rule (one per section).
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..data.stats_db import StatsDB


@dataclass
class Doc:
    id: str
    text: str
    meta: dict


def _score(inn: dict) -> str:
    b = inn["balls"]
    return f"{inn['runs']}/{inn['wickets']} ({b // 6}.{b % 6} overs)"


def match_docs(db: StatsDB) -> list[Doc]:
    docs = []
    finals = {r["season"]: r["id"] for r in db.q(
        "SELECT season, id FROM matches m WHERE id = (SELECT id FROM matches WHERE season = m.season ORDER BY date DESC, id DESC LIMIT 1)")}
    for m in db.all_matches():
        mid = m["id"]
        is_final = finals.get(m["season"]) == mid
        inns = {i["inning"]: i for i in db.innings_summary(mid)}
        scores = "; ".join(f"{inns[n]['batting_team']} {_score(inns[n])}" for n in sorted(inns) if n <= 2)
        if m["winner"]:
            margin = f"{m['win_by_runs']} runs" if m["win_by_runs"] else f"{m['win_by_wickets']} wickets"
            result = f"{m['winner']} won by {margin}" + (" (Duckworth-Lewis)" if m["dl_applied"] else "")
        else:
            result = "No result"
        bats = ", ".join(f"{r['batsman']} {r['runs']} off {r['balls']}" for r in db.top_scorers_in_match(mid))
        bowls = ", ".join(f"{r['bowler']} {r['wickets']}/{r['runs']}" for r in db.top_bowlers_in_match(mid))
        stage = f"IPL {m['season']} FINAL, the title decider: {m['winner']} became champions. " if is_final else ""
        text = (f"{stage}IPL {m['season']} {'final' if is_final else 'match'} on {m['date']} at {m['venue']}, {m['city']}: {m['team1']} vs {m['team2']}. "
                f"{m['toss_winner']} won the toss and chose to {m['toss_decision']}. Scores: {scores}. Result: {result}. "
                f"Player of the match: {m['player_of_match'] or 'none'}. Top batters: {bats}. Top bowlers: {bowls}.")
        docs.append(Doc(f"match-{mid}", text, {"type": "match", "season": m["season"], "match_id": mid,
                                               "teams": f"{m['team1']}|{m['team2']}", "stage": "final" if is_final else "league"}))
    return docs


def season_docs(db: StatsDB) -> list[Doc]:
    docs = []
    for season in range(2008, 2018):
        final = db.q("SELECT * FROM matches WHERE season = ? ORDER BY date DESC, id DESC LIMIT 1", (season,))
        if not final:
            continue
        f = final[0]
        runners_up = f["team2"] if f["winner"] == f["team1"] else f["team1"]
        runs = ", ".join(f"{r['player']} ({r['runs']})" for r in db.top("runs", season, 5))
        wkts = ", ".join(f"{r['player']} ({r['wickets']})" for r in db.top("wickets", season, 5))
        n = db.q("SELECT COUNT(*) AS n FROM matches WHERE season = ?", (season,))[0]["n"]
        text = (f"IPL {season} season: {n} matches. Champion: {f['winner']}, beating {runners_up} in the final on "
                f"{f['date']} at {f['venue']}. Most runs (orange cap race): {runs}. Most wickets (purple cap race): {wkts}.")
        docs.append(Doc(f"season-{season}", text, {"type": "season", "season": season}))
    return docs


def team_docs(db: StatsDB) -> list[Doc]:
    titles: dict[str, list[int]] = defaultdict(list)
    for season in range(2008, 2018):
        r = db.team_season(db.teams[0], season)["season_champion"]
        if r:
            titles[r].append(season)
    docs = []
    for team in sorted(db.teams):
        r = db.q("""SELECT COUNT(*) AS played, SUM(CASE WHEN winner = ? THEN 1 ELSE 0 END) AS won,
                MIN(season) AS first, MAX(season) AS last FROM matches WHERE team1 = ? OR team2 = ?""", (team, team, team))[0]
        t = titles.get(team, [])
        text = (f"{team} played {r['played']} IPL matches between {r['first']} and {r['last']} and won {r['won']}. "
                f"Titles: {', '.join(map(str, t)) if t else 'none in 2008-2017'}.")
        docs.append(Doc(f"team-{re.sub(r'[^a-z]+', '-', team.lower())}", text, {"type": "team"}))
    return docs


def player_docs(db: StatsDB, limit: int = 150) -> list[Doc]:
    active = db.q("""SELECT name FROM (
            SELECT batsman AS name, COUNT(*) AS n FROM deliveries GROUP BY batsman
            UNION ALL SELECT bowler, COUNT(*) FROM deliveries GROUP BY bowler)
        GROUP BY name ORDER BY SUM(n) DESC LIMIT ?""", (limit,))
    docs = []
    for row in active:
        p = row["name"]
        bat, bowl = db.batting(p), db.bowling(p)
        teams = ", ".join(r["t"] for r in db.q(
            "SELECT DISTINCT batting_team AS t FROM deliveries WHERE batsman = ? UNION SELECT DISTINCT bowling_team FROM deliveries WHERE bowler = ?", (p, p)))
        parts = [f"{p} played IPL cricket for {teams}."]
        if bat["balls"]:
            parts.append(f"Batting 2008-2017: {bat['runs']} runs from {bat['balls']} balls in {bat['matches']} matches, "
                         f"average {bat['average']}, strike rate {bat['strike_rate']}, highest score {bat['highest_score']}, "
                         f"{bat['fours']} fours and {bat['sixes']} sixes.")
        if bowl["wickets"]:
            parts.append(f"Bowling: {bowl['wickets']} wickets, economy {bowl['economy']}, average {bowl['average']}.")
        docs.append(Doc(f"player-{re.sub(r'[^a-z0-9]+', '-', p.lower())}", " ".join(parts), {"type": "player"}))
    return docs


def rule_docs(path: Path) -> list[Doc]:
    docs = []
    for block in re.split(r"(?m)^# ", path.read_text()):
        block = block.strip()
        if not block:
            continue
        title, _, body = block.partition("\n")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        docs.append(Doc(f"rule-{slug}", f"{title}. {body.strip()}", {"type": "rule", "title": title}))
    return docs


def build_all(db: StatsDB, rules_path: Path) -> list[Doc]:
    return rule_docs(rules_path) + season_docs(db) + team_docs(db) + player_docs(db) + match_docs(db)
