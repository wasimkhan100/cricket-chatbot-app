#!/usr/bin/env python3
"""End-to-end answer check: ask the running chatbot questions and compare each reply with facts read
directly from the database (not from the chatbot), so the check is independent of the pipeline.

Usage: python3 scripts/answer_eval.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
DB = Path(__file__).resolve().parents[1] / "backend" / "var" / "cricket.sqlite3"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
q1 = lambda sql, *a: c.execute(sql, a).fetchone()


def final(season):
    return q1("SELECT winner, player_of_match FROM matches WHERE season=? ORDER BY date DESC, id DESC LIMIT 1", season)


def runs(player, season=None):
    sql = "SELECT SUM(d.batsman_runs) FROM deliveries d JOIN matches m ON m.id=d.match_id WHERE d.batsman=?"
    return q1(sql + (" AND m.season=?" if season else ""), *((player, season) if season else (player,)))[0]


def wickets(player, season=None):
    sql = ("SELECT COUNT(*) FROM deliveries d JOIN matches m ON m.id=d.match_id WHERE d.bowler=? AND d.player_dismissed!='' "
           "AND d.dismissal_kind NOT IN ('run out','retired hurt','obstructing the field')")
    return q1(sql + (" AND m.season=?" if season else ""), *((player, season) if season else (player,)))[0]


def top_wickets(season):
    return q1("SELECT d.bowler, COUNT(*) n FROM deliveries d JOIN matches m ON m.id=d.match_id WHERE m.season=? AND d.player_dismissed!='' "
              "AND d.dismissal_kind NOT IN ('run out','retired hurt','obstructing the field') GROUP BY d.bowler ORDER BY n DESC LIMIT 1", season)


def h2h(a, b):
    return {w: n for w, n in c.execute("SELECT winner, COUNT(*) FROM matches WHERE (team1=? AND team2=?) OR (team1=? AND team2=?) GROUP BY winner", (a, b, b, a))}


mi_csk = h2h("Mumbai Indians", "Chennai Super Kings")
tw2011 = top_wickets(2011)
sixes_gayle_2012 = q1("SELECT COUNT(*) FROM deliveries d JOIN matches m ON m.id=d.match_id WHERE d.batsman='CH Gayle' AND d.batsman_runs=6 AND m.season=2012")[0]
srh2016 = q1("SELECT COUNT(*), SUM(winner='Sunrisers Hyderabad') FROM matches WHERE season=2016 AND (team1='Sunrisers Hyderabad' OR team2='Sunrisers Hyderabad')")

# (question, required facts [, forbidden strings]); each required fact is a list of acceptable spellings
CASES = [
    ("Who won IPL 2010?", [[final(2010)[0]]]),
    ("Which team won the 2016 IPL final?", [[final(2016)[0]]]),
    ("Who won the IPL in 2013?", [[final(2013)[0]]]),
    ("Did Deccan Chargers ever win the IPL?", [["2009"]], ["2011", "2010", "2012"]),
    ("How many IPL titles have Chennai Super Kings won?", [["2 times", "two", "2010, 2011", "2010 and 2011"]], ["3 times", "4 times", "three times", "four times"]),
    ("Have Royal Challengers Bangalore ever won the IPL?", [["no", "not", "never", "haven't", "none"]]),
    ("Who was player of the match in the 2017 final?", [[final(2017)[1], "Krunal Pandya"]]),
    ("How many runs did Virat Kohli score in IPL 2016?", [[str(runs("V Kohli", 2016))]]),
    ("How many IPL runs has Virat Kohli scored in his career?", [[str(runs("V Kohli")), f"{runs('V Kohli'):,}"]]),
    ("How many IPL wickets has Lasith Malinga taken?", [[str(wickets("SL Malinga"))]]),
    ("Who took the most wickets in IPL 2011?", [[tw2011[0], "Malinga"], [str(tw2011[1])]]),
    ("Head to head record of Mumbai Indians vs Chennai Super Kings", [[str(mi_csk["Mumbai Indians"])], [str(mi_csk["Chennai Super Kings"])]]),
    ("Who scored the most runs in IPL 2010?", [["Tendulkar"], [str(runs("SR Tendulkar", 2010))]]),
    ("How many sixes did Chris Gayle hit in IPL 2012?", [[str(sixes_gayle_2012)]]),
    ("How many matches did Sunrisers Hyderabad win in 2016?", [[str(srh2016[1])]]),
    ("What is a free hit?", [["no ball", "no-ball"]]),
    ("How many overs can one bowler bowl in a T20?", [["four", "4"]]),
    ("What happens if a T20 match is tied?", [["super over"]]),
    ("What is DRS?", [["review"]]),
    ("How is the target revised when rain shortens a match?", [["Duckworth", "DLS"]]),
    ("What's the score?", "LIVE"),
    ("Who won the toss?", "TOSS"),
]


def session() -> str:
    r = urllib.request.urlopen(urllib.request.Request(f"{BASE}/api/session", method="POST"))
    return json.load(r)["token"]


def ask(token: str, q: str) -> dict:
    req = urllib.request.Request(f"{BASE}/api/chat", data=json.dumps({"question": q}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if line.startswith(b"data: "):
                e = json.loads(line[6:])
                if e["type"] == "done":
                    return e
    return {}


def main() -> None:
    passed, rows = 0, []
    for i, case in enumerate(CASES):
        q, facts, forbidden = case if len(case) == 3 else (*case, [])
        token = session() if i % 5 == 0 else token  # stay under the per-fan rate limit
        done = ask(token, q)
        ans = done.get("answer", "")
        if facts == "LIVE":
            live = json.load(urllib.request.urlopen(f"{BASE}/api/live"))
            inn = live["innings"][-1] if live["innings"] else None
            facts = [[f"{inn['runs']}/{inn['wickets']}"]] if inn else [["hasn't started"]]
        elif facts == "TOSS":
            live = json.load(urllib.request.urlopen(f"{BASE}/api/live"))
            facts = [[live["toss"].split(" won")[0]]]
        missing = [alts[0] for alts in facts if not any(re.search(rf"\b{re.escape(a.lower())}", ans.lower()) for a in alts)]
        # A false claim fails the case even when the right fact is also there (e.g. "they also won in 2011").
        false_claims = [f for f in forbidden if f in ans]
        missing += [f"false claim: {f}" for f in false_claims]
        ok = not missing
        passed += ok
        rows.append((ok, q, ans, done.get("path"), done.get("verified"), missing))
        print(f"{'PASS' if ok else 'FAIL'}  [{done.get('path')}] {q}\n      -> {ans[:180]}" + (f"\n      missing: {missing}" if missing else ""))
        time.sleep(0.5)
    print(f"\n{passed}/{len(CASES)} answers contain the correct facts")


if __name__ == "__main__":
    main()
