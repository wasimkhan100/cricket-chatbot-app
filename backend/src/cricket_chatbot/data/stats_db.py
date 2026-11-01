"""Historical IPL stats (2008-2017) in SQLite, built from the CSVs in Cricket-data/.

The query path opens the database read-only and only runs the fixed, parameterised queries below.
The LLM never writes SQL.
"""

import csv
import difflib
import sqlite3
import threading
from pathlib import Path

TEAM_ALIASES = {
    "mi": "Mumbai Indians", "mumbai": "Mumbai Indians",
    "csk": "Chennai Super Kings", "chennai": "Chennai Super Kings",
    "rcb": "Royal Challengers Bangalore", "bangalore": "Royal Challengers Bangalore", "bengaluru": "Royal Challengers Bangalore",
    "kkr": "Kolkata Knight Riders", "kolkata": "Kolkata Knight Riders",
    "srh": "Sunrisers Hyderabad", "sunrisers": "Sunrisers Hyderabad",
    "dc": "Deccan Chargers", "deccan": "Deccan Chargers",
    "dd": "Delhi Daredevils", "delhi": "Delhi Daredevils",
    "kxip": "Kings XI Punjab", "punjab": "Kings XI Punjab",
    "rr": "Rajasthan Royals", "rajasthan": "Rajasthan Royals",
    "rps": "Rising Pune Supergiant", "pune": "Rising Pune Supergiant", "rising pune supergiants": "Rising Pune Supergiant",
    "pwi": "Pune Warriors", "gl": "Gujarat Lions", "gujarat": "Gujarat Lions", "ktk": "Kochi Tuskers Kerala", "kochi": "Kochi Tuskers Kerala",
}
# Fans use first names and nicknames; the scorecard uses initials.
PLAYER_ALIASES = {
    "sachin": "SR Tendulkar", "tendulkar": "SR Tendulkar", "virat": "V Kohli", "rohit": "RG Sharma",
    "rohit sharma": "RG Sharma", "dhoni": "MS Dhoni", "msd": "MS Dhoni", "mahi": "MS Dhoni", "abd": "AB de Villiers",
    "ab de villiers": "AB de Villiers", "de villiers": "AB de Villiers", "bumrah": "JJ Bumrah", "bhuvi": "B Kumar",
    "bhuvneshwar": "B Kumar", "yuvraj": "Yuvraj Singh", "yuvi": "Yuvraj Singh", "hardik": "HH Pandya",
    "krunal": "KH Pandya", "jadeja": "RA Jadeja", "jaddu": "RA Jadeja", "ashwin": "R Ashwin", "sehwag": "V Sehwag",
    "gambhir": "G Gambhir", "dravid": "R Dravid", "ganguly": "SC Ganguly", "dada": "SC Ganguly", "pollard": "KA Pollard",
    "steve smith": "SPD Smith", "warner": "DA Warner", "gayle": "CH Gayle", "raina": "SK Raina", "dhawan": "S Dhawan",
    "malinga": "SL Malinga",
    # Devanagari spellings, matched as plain substrings (word boundaries don't work inside Devanagari words).
    "कोहली": "V Kohli", "विराट": "V Kohli", "धोनी": "MS Dhoni", "रोहित": "RG Sharma", "तेंदुलकर": "SR Tendulkar",
    "सचिन": "SR Tendulkar", "रैना": "SK Raina", "गेल": "CH Gayle", "बुमराह": "JJ Bumrah", "जडेजा": "RA Jadeja",
    "अश्विन": "R Ashwin", "युवराज": "Yuvraj Singh", "गंभीर": "G Gambhir", "सहवाग": "V Sehwag", "मलिंगा": "SL Malinga", "narine": "SP Narine", "harbhajan": "Harbhajan Singh", "bhajji": "Harbhajan Singh",
    "zaheer": "Z Khan", "rahane": "AM Rahane", "uthappa": "RV Uthappa", "watson": "SR Watson", "russell": "AD Russell",
}
_CANON = {"Rising Pune Supergiants": "Rising Pune Supergiant"}
NOT_BOWLER_WICKETS = ("run out", "retired hurt", "obstructing the field", "")


def _team(name: str) -> str:
    return _CANON.get(name, name)


def build_database(data_dir: Path, db_path: Path) -> None:
    """Idempotent: rebuilds into a temp file and swaps it in, so readers never see a half-built DB."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE matches (id INTEGER PRIMARY KEY, season INTEGER, city TEXT, date TEXT, team1 TEXT, team2 TEXT,
            toss_winner TEXT, toss_decision TEXT, result TEXT, dl_applied INTEGER, winner TEXT, win_by_runs INTEGER,
            win_by_wickets INTEGER, player_of_match TEXT, venue TEXT);
        CREATE TABLE deliveries (match_id INTEGER, inning INTEGER, batting_team TEXT, bowling_team TEXT, over INTEGER,
            ball INTEGER, batsman TEXT, non_striker TEXT, bowler TEXT, is_super_over INTEGER, wide_runs INTEGER,
            bye_runs INTEGER, legbye_runs INTEGER, noball_runs INTEGER, penalty_runs INTEGER, batsman_runs INTEGER,
            extra_runs INTEGER, total_runs INTEGER, player_dismissed TEXT, dismissal_kind TEXT, fielder TEXT);
    """)
    with open(data_dir / "matches.csv", newline="") as f:
        rows = [(int(r["id"]), int(r["season"]), r["city"], r["date"], _team(r["team1"]), _team(r["team2"]),
                 _team(r["toss_winner"]), r["toss_decision"], r["result"], int(r["dl_applied"] or 0), _team(r["winner"]),
                 int(r["win_by_runs"] or 0), int(r["win_by_wickets"] or 0), r["player_of_match"], r["venue"])
                for r in csv.DictReader(f)]
    con.executemany("INSERT INTO matches VALUES (" + ",".join("?" * 15) + ")", rows)
    ints = ("match_id", "inning", "over", "ball", "is_super_over", "wide_runs", "bye_runs", "legbye_runs", "noball_runs",
            "penalty_runs", "batsman_runs", "extra_runs", "total_runs")
    cols = ("match_id", "inning", "batting_team", "bowling_team", "over", "ball", "batsman", "non_striker", "bowler",
            "is_super_over", "wide_runs", "bye_runs", "legbye_runs", "noball_runs", "penalty_runs", "batsman_runs",
            "extra_runs", "total_runs", "player_dismissed", "dismissal_kind", "fielder")
    with open(data_dir / "deliveries.csv", newline="") as f:
        batch = []
        for r in csv.DictReader(f):
            vals = []
            for c in cols:
                v = r[c]
                if c in ints:
                    vals.append(int(v or 0))
                elif c in ("batting_team", "bowling_team"):
                    vals.append(_team(v))
                else:
                    vals.append(v)
            batch.append(vals)
        con.executemany("INSERT INTO deliveries VALUES (" + ",".join("?" * len(cols)) + ")", batch)
    con.executescript("""
        CREATE INDEX ix_del_batsman ON deliveries(batsman);
        CREATE INDEX ix_del_bowler ON deliveries(bowler);
        CREATE INDEX ix_del_match ON deliveries(match_id, inning, over, ball);
        CREATE INDEX ix_m_season ON matches(season);
        CREATE TABLE players AS SELECT name, SUM(n) AS appearances FROM (
            SELECT batsman AS name, COUNT(*) AS n FROM deliveries GROUP BY batsman
            UNION ALL SELECT bowler, COUNT(*) FROM deliveries GROUP BY bowler) GROUP BY name;
    """)
    con.commit()
    con.close()
    tmp.replace(db_path)


class StatsDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._players: list[str] | None = None
        self._teams: list[str] | None = None

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._con().execute(sql, args).fetchall()]

    def ping(self) -> bool:
        return self.q("SELECT 1 AS ok")[0]["ok"] == 1

    # --- name resolution -------------------------------------------------------
    @property
    def players(self) -> list[str]:
        if self._players is None:
            # Most active first, so 'Kohli' resolves to V Kohli before T Kohli.
            self._players = [r["name"] for r in self.q("SELECT name FROM players ORDER BY appearances DESC")]
        return self._players

    @property
    def teams(self) -> list[str]:
        if self._teams is None:
            self._teams = [r["t"] for r in self.q("SELECT DISTINCT team1 AS t FROM matches")]
        return self._teams

    def resolve_player(self, name: str) -> list[str]:
        """'Dhoni' -> ['MS Dhoni']. Returns up to 3 candidates, best first."""
        n = name.strip().lower()
        if not n:
            return []
        if n in PLAYER_ALIASES:
            return [PLAYER_ALIASES[n]]
        exact = [p for p in self.players if p.lower() == n]
        if exact:
            return exact
        surname = [p for p in self.players if p.lower().split()[-1] == n.split()[-1]
                   and (len(n.split()) == 1 or p.lower().split()[0][0] == n.split()[0][0])]
        contains = [p for p in self.players if n in p.lower()]
        fuzzy = difflib.get_close_matches(name, self.players, n=3, cutoff=0.6)
        seen, out = set(), []
        for p in surname + contains + fuzzy:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out[:3]

    def resolve_team(self, name: str) -> str | None:
        n = name.strip().lower()
        if n in TEAM_ALIASES:
            return TEAM_ALIASES[n]
        for t in self.teams:
            if t.lower() == n or n in t.lower():
                return t
        m = difflib.get_close_matches(name, self.teams, n=1, cutoff=0.5)
        return m[0] if m else None

    # --- stats ------------------------------------------------------------------
    def batting(self, player: str, season: int | None = None) -> dict:
        where, args = "d.batsman = ?", [player]
        if season:
            where += " AND m.season = ?"
            args.append(season)
        agg = self.q(f"""
            SELECT COUNT(DISTINCT d.match_id) AS matches, SUM(d.batsman_runs) AS runs,
                   SUM(CASE WHEN d.wide_runs = 0 THEN 1 ELSE 0 END) AS balls,
                   SUM(CASE WHEN d.batsman_runs = 4 THEN 1 ELSE 0 END) AS fours,
                   SUM(CASE WHEN d.batsman_runs = 6 THEN 1 ELSE 0 END) AS sixes
            FROM deliveries d JOIN matches m ON m.id = d.match_id WHERE {where}""", tuple(args))[0]
        outs = self.q(f"""SELECT COUNT(*) AS n FROM deliveries d JOIN matches m ON m.id = d.match_id
            WHERE d.player_dismissed = ? {'AND m.season = ?' if season else ''}""",
                      (player, season) if season else (player,))[0]["n"]
        hs = self.q(f"""SELECT d.match_id, SUM(d.batsman_runs) AS r FROM deliveries d JOIN matches m ON m.id = d.match_id
            WHERE {where} GROUP BY d.match_id ORDER BY r DESC LIMIT 1""", tuple(args))
        runs, balls = agg["runs"] or 0, agg["balls"] or 0
        return {
            "player": player, "season": season or "all (2008-2017)", "matches": agg["matches"], "runs": runs,
            "balls": balls, "outs": outs, "fours": agg["fours"] or 0, "sixes": agg["sixes"] or 0,
            "average": round(runs / outs, 2) if outs else None,
            "strike_rate": round(100 * runs / balls, 2) if balls else None,
            "highest_score": hs[0]["r"] if hs else None,
        }

    def bowling(self, player: str, season: int | None = None) -> dict:
        where, args = "d.bowler = ?", [player]
        if season:
            where += " AND m.season = ?"
            args.append(season)
        placeholders = ",".join("?" * len(NOT_BOWLER_WICKETS))
        agg = self.q(f"""
            SELECT COUNT(DISTINCT d.match_id) AS matches,
                   SUM(d.total_runs - d.bye_runs - d.legbye_runs) AS runs,
                   SUM(CASE WHEN d.wide_runs = 0 AND d.noball_runs = 0 THEN 1 ELSE 0 END) AS balls,
                   SUM(CASE WHEN d.dismissal_kind NOT IN ({placeholders}) THEN 1 ELSE 0 END) AS wickets
            FROM deliveries d JOIN matches m ON m.id = d.match_id WHERE {where}""",
                     tuple(NOT_BOWLER_WICKETS) + tuple(args))[0]
        runs, balls, wk = agg["runs"] or 0, agg["balls"] or 0, agg["wickets"] or 0
        return {
            "player": player, "season": season or "all (2008-2017)", "matches": agg["matches"], "wickets": wk,
            "runs_conceded": runs, "overs": f"{balls // 6}.{balls % 6}",
            "economy": round(6 * runs / balls, 2) if balls else None,
            "average": round(runs / wk, 2) if wk else None,
        }

    def head_to_head(self, team_a: str, team_b: str) -> dict:
        rows = self.q("""SELECT winner, COUNT(*) AS n FROM matches
            WHERE (team1 = ? AND team2 = ?) OR (team1 = ? AND team2 = ?) GROUP BY winner""",
                      (team_a, team_b, team_b, team_a))
        wins = {r["winner"] or "no result": r["n"] for r in rows}
        return {"team_a": team_a, "team_b": team_b, "matches": sum(wins.values()), "wins": wins}

    def team_season(self, team: str, season: int) -> dict:
        r = self.q("""SELECT COUNT(*) AS played, SUM(CASE WHEN winner = ? THEN 1 ELSE 0 END) AS won
            FROM matches WHERE season = ? AND (team1 = ? OR team2 = ?)""", (team, season, team, team))[0]
        final = self.q("SELECT team1, team2, winner FROM matches WHERE season = ? ORDER BY date DESC, id DESC LIMIT 1", (season,))
        return {"team": team, "season": season, "played": r["played"], "won": r["won"] or 0,
                "season_champion": final[0]["winner"] if final else None}

    def season_summary(self, season: int) -> dict:
        final = self.q("SELECT * FROM matches WHERE season = ? ORDER BY date DESC, id DESC LIMIT 1", (season,))
        if not final:
            return {"season": season, "error": "no data for this season (dataset covers 2008-2017)"}
        f = final[0]
        return {
            "season": season,
            "matches": self.q("SELECT COUNT(*) AS n FROM matches WHERE season = ?", (season,))[0]["n"],
            "champion": f["winner"],
            "runner_up": f["team2"] if f["winner"] == f["team1"] else f["team1"],
            "final": {"date": f["date"], "venue": f["venue"], "player_of_match": f["player_of_match"],
                      "margin": f"{f['win_by_runs']} runs" if f["win_by_runs"] else f"{f['win_by_wickets']} wickets"},
            "most_runs": self.top("runs", season, 3),
            "most_wickets": self.top("wickets", season, 3),
        }

    def team_titles(self, team: str) -> dict:
        finals = self.q("""SELECT season, team1, team2, winner, win_by_runs, win_by_wickets FROM matches m
            WHERE id = (SELECT id FROM matches WHERE season = m.season ORDER BY date DESC, id DESC LIMIT 1)
              AND (team1 = ? OR team2 = ?) ORDER BY season""", (team, team))
        won = [f["season"] for f in finals if f["winner"] == team]
        lost = [f["season"] for f in finals if f["winner"] != team]
        return {"team": team, "titles_won": len(won), "title_seasons": won or "none",
                "finals_lost": lost or "none", "data_covers": "IPL 2008-2017"}

    def milestone(self, category: str, threshold: int) -> dict:
        """Everyone with at least `threshold` career runs, wickets or sixes (2008-2017)."""
        placeholders = ",".join("?" * len(NOT_BOWLER_WICKETS))
        sql = {
            "runs": "SELECT batsman AS player, SUM(batsman_runs) AS n FROM deliveries GROUP BY batsman",
            "sixes": "SELECT batsman AS player, SUM(batsman_runs = 6) AS n FROM deliveries GROUP BY batsman",
            "wickets": f"SELECT bowler AS player, COUNT(*) AS n FROM deliveries WHERE player_dismissed != '' "
                       f"AND dismissal_kind NOT IN ({placeholders}) GROUP BY bowler",
        }[category]
        args = tuple(NOT_BOWLER_WICKETS) if category == "wickets" else ()
        rows = self.q(f"SELECT player, n FROM ({sql}) WHERE n >= ? ORDER BY n DESC", args + (threshold,))
        return {"category": category, "threshold": threshold, "count": len(rows), "players": rows[:15]}

    def top(self, category: str, season: int | None = None, limit: int = 5) -> list[dict]:
        limit = max(1, min(limit, 10))
        season_sql = "AND m.season = ?" if season else ""
        args: tuple = (season,) if season else ()
        if category == "wickets":
            placeholders = ",".join("?" * len(NOT_BOWLER_WICKETS))
            return self.q(f"""SELECT d.bowler AS player, COUNT(*) AS wickets FROM deliveries d JOIN matches m ON m.id = d.match_id
                WHERE d.player_dismissed != '' AND d.dismissal_kind NOT IN ({placeholders}) {season_sql}
                GROUP BY d.bowler ORDER BY wickets DESC LIMIT ?""", tuple(NOT_BOWLER_WICKETS) + args + (limit,))
        if category == "sixes":
            return self.q(f"""SELECT d.batsman AS player, COUNT(*) AS sixes FROM deliveries d JOIN matches m ON m.id = d.match_id
                WHERE d.batsman_runs = 6 {season_sql} GROUP BY d.batsman ORDER BY sixes DESC LIMIT ?""", args + (limit,))
        return self.q(f"""SELECT d.batsman AS player, SUM(d.batsman_runs) AS runs FROM deliveries d JOIN matches m ON m.id = d.match_id
            WHERE 1=1 {season_sql} GROUP BY d.batsman ORDER BY runs DESC LIMIT ?""", args + (limit,))

    def match(self, match_id: int) -> dict | None:
        rows = self.q("SELECT * FROM matches WHERE id = ?", (match_id,))
        return rows[0] if rows else None

    def deliveries(self, match_id: int) -> list[dict]:
        return self.q("SELECT * FROM deliveries WHERE match_id = ? AND is_super_over = 0 ORDER BY inning, over, ball, rowid",
                      (match_id,))

    def all_matches(self) -> list[dict]:
        return self.q("SELECT * FROM matches ORDER BY date")

    def innings_summary(self, match_id: int) -> list[dict]:
        return self.q("""SELECT inning, batting_team, SUM(total_runs) AS runs,
                SUM(CASE WHEN player_dismissed != '' THEN 1 ELSE 0 END) AS wickets,
                SUM(CASE WHEN wide_runs = 0 AND noball_runs = 0 THEN 1 ELSE 0 END) AS balls
            FROM deliveries WHERE match_id = ? AND is_super_over = 0 GROUP BY inning, batting_team ORDER BY inning""", (match_id,))

    def top_scorers_in_match(self, match_id: int, limit: int = 2) -> list[dict]:
        return self.q("""SELECT batsman, batting_team, SUM(batsman_runs) AS runs,
                SUM(CASE WHEN wide_runs = 0 THEN 1 ELSE 0 END) AS balls
            FROM deliveries WHERE match_id = ? GROUP BY batsman ORDER BY runs DESC LIMIT ?""", (match_id, limit))

    def top_bowlers_in_match(self, match_id: int, limit: int = 2) -> list[dict]:
        placeholders = ",".join("?" * len(NOT_BOWLER_WICKETS))
        return self.q(f"""SELECT bowler, bowling_team, SUM(CASE WHEN dismissal_kind NOT IN ({placeholders}) THEN 1 ELSE 0 END) AS wickets,
                SUM(total_runs - bye_runs - legbye_runs) AS runs
            FROM deliveries WHERE match_id = ? GROUP BY bowler ORDER BY wickets DESC, runs ASC LIMIT ?""",
                      tuple(NOT_BOWLER_WICKETS) + (match_id, limit))

    # --- deeper stats for the multi-agent tools ---------------------------------------
    @property
    def venues(self) -> list[str]:
        if getattr(self, "_venues", None) is None:
            self._venues = [r["venue"] for r in self.q("SELECT venue, COUNT(*) n FROM matches GROUP BY venue ORDER BY n DESC")]
        return self._venues

    def resolve_venue(self, name: str) -> str | None:
        """Venue or city name -> the stadium with the most matches there."""
        n = name.strip().lower()
        if not n:
            return None
        for v in self.venues:
            if n in v.lower():
                return v
        city = self.q("SELECT venue, COUNT(*) n FROM matches WHERE lower(city) = ? GROUP BY venue ORDER BY n DESC LIMIT 1", (n,))
        if city:
            return city[0]["venue"]
        m = difflib.get_close_matches(name, self.venues, n=1, cutoff=0.5)
        return m[0] if m else None

    def venue_stats(self, venue: str) -> dict:
        m = self.q("""SELECT COUNT(*) AS matches, MIN(city) AS city,
                SUM(CASE WHEN win_by_runs > 0 THEN 1 ELSE 0 END) AS won_batting_first,
                SUM(CASE WHEN win_by_wickets > 0 THEN 1 ELSE 0 END) AS won_chasing,
                MIN(season) AS first, MAX(season) AS last FROM matches WHERE venue = ?""", (venue,))[0]
        first_inns = self.q("""SELECT AVG(r) AS avg, MAX(r) AS best FROM (SELECT d.match_id, SUM(d.total_runs) AS r
                FROM deliveries d JOIN matches m ON m.id = d.match_id
                WHERE m.venue = ? AND d.inning = 1 GROUP BY d.match_id)""", (venue,))[0]
        top = self.q("""SELECT d.batsman AS player, SUM(d.batsman_runs) AS runs FROM deliveries d JOIN matches m ON m.id = d.match_id
                WHERE m.venue = ? GROUP BY d.batsman ORDER BY runs DESC LIMIT 3""", (venue,))
        decided = (m["won_batting_first"] or 0) + (m["won_chasing"] or 0)
        bf_pct = round(100 * (m["won_batting_first"] or 0) / decided, 1) if decided else None
        return {"venue": venue, "city": m["city"], "seasons": f"{m['first']}-{m['last']}", "matches": m["matches"],
                "won_batting_first": m["won_batting_first"], "won_chasing": m["won_chasing"],
                "batting_first_win_pct": bf_pct, "chasing_win_pct": round(100 - bf_pct, 1) if bf_pct is not None else None,
                "better_record": ("chasing" if bf_pct < 50 else "batting first" if bf_pct > 50 else "even") if bf_pct is not None else None,
                "note": "all IPL matches at this venue 2008-2017, not one team's record",
                "average_first_innings": round(first_inns["avg"]) if first_inns["avg"] else None,
                "highest_first_innings": first_inns["best"], "top_run_scorers": top}

    def toss_impact(self, season: int | None = None, team: str | None = None) -> dict:
        where, args = ["result = 'normal'"], []
        if season:
            where.append("season = ?")
            args.append(season)
        if team:
            where.append("toss_winner = ?")
            args.append(team)
        rows = self.q(f"""SELECT toss_decision AS decision, COUNT(*) AS tosses,
                SUM(CASE WHEN toss_winner = winner THEN 1 ELSE 0 END) AS toss_winner_won
            FROM matches WHERE {' AND '.join(where)} GROUP BY toss_decision""", tuple(args))
        total = sum(r["tosses"] for r in rows)
        won = sum(r["toss_winner_won"] for r in rows)
        dec = {r["decision"]: r for r in rows}
        pct = lambda d: round(100 * dec[d]["toss_winner_won"] / dec[d]["tosses"], 1) if d in dec and dec[d]["tosses"] else None
        summary = (f"Toss winners won {round(100 * won / total, 1) if total else '-'}% of their matches. "
                   f"Toss winners who chose to field won {pct('field')}% of the time; those who chose to bat won {pct('bat')}%.")
        return {"summary": summary, "season": season or "2008-2017", "team": team or "all teams", "tosses": total,
                "toss_winner_won_match": won, "toss_win_pct": round(100 * won / total, 1) if total else None,
                "by_decision": [{**r, "win_pct": round(100 * r["toss_winner_won"] / r["tosses"], 1)} for r in rows]}

    def batter_vs_bowler(self, batter: str, bowler: str) -> dict:
        r = self.q("""SELECT COUNT(DISTINCT match_id) AS matches, SUM(batsman_runs) AS runs,
                SUM(CASE WHEN wide_runs = 0 THEN 1 ELSE 0 END) AS balls,
                SUM(CASE WHEN batsman_runs = 4 THEN 1 ELSE 0 END) AS fours,
                SUM(CASE WHEN batsman_runs = 6 THEN 1 ELSE 0 END) AS sixes,
                SUM(CASE WHEN player_dismissed = batsman AND dismissal_kind NOT IN ('run out', 'retired hurt', 'obstructing the field')
                         THEN 1 ELSE 0 END) AS dismissals
            FROM deliveries WHERE batsman = ? AND bowler = ?""", (batter, bowler))[0]
        balls = r["balls"] or 0
        sr = round(100 * (r["runs"] or 0) / balls, 1) if balls else None
        summary = (f"{batter} scored {r['runs'] or 0} runs off {balls} balls from {bowler} (strike rate {sr}) and was dismissed "
                   f"by him {r['dismissals'] or 0} times." if balls else f"{batter} never faced {bowler} in this data.")
        return {"summary": summary, "batter": batter, "bowler": bowler, "matches": r["matches"], "balls": balls, "runs": r["runs"] or 0,
                "fours": r["fours"] or 0, "sixes": r["sixes"] or 0, "dismissals": r["dismissals"] or 0,
                "strike_rate": round(100 * (r["runs"] or 0) / balls, 1) if balls else None}

    def dismissals(self, player: str) -> dict:
        out = self.q("""SELECT dismissal_kind AS how, COUNT(*) AS n FROM deliveries WHERE player_dismissed = ?
                GROUP BY dismissal_kind ORDER BY n DESC""", (player,))
        by = self.q("""SELECT bowler, COUNT(*) AS n FROM deliveries WHERE player_dismissed = ?
                AND dismissal_kind NOT IN ('run out', 'retired hurt', 'obstructing the field')
                GROUP BY bowler ORDER BY n DESC LIMIT 3""", (player,))
        taken = self.q(f"""SELECT dismissal_kind AS how, COUNT(*) AS n FROM deliveries WHERE bowler = ?
                AND dismissal_kind NOT IN ({','.join('?' * len(NOT_BOWLER_WICKETS))}) GROUP BY dismissal_kind ORDER BY n DESC""",
                       (player, *NOT_BOWLER_WICKETS))
        total = sum(r["n"] for r in out)
        ranked = ", ".join(f"{r['how']} {r['n']}" for r in out[:3])
        summary = (f"{player} was out {total} times; most often {out[0]['how']} ({out[0]['n']} of {total}). Top three: {ranked}."
                   if out else f"{player} was never dismissed in this data.")
        return {"summary": summary, "player": player, "got_out_by": out, "dismissed_most_by": by, "wickets_taken_by": taken or "none"}

    def find_matches(self, team_a: str | None, team_b: str | None, season: int | None, limit: int = 10) -> dict:
        where, args = [], []
        for t in (team_a, team_b):
            if t:
                where.append("(team1 = ? OR team2 = ?)")
                args += [t, t]
        if season:
            where.append("season = ?")
            args.append(season)
        rows = self.q(f"""SELECT id, season, date, venue, team1, team2, winner, win_by_runs, win_by_wickets, player_of_match
            FROM matches {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY date LIMIT ?""", (*args, limit + 1))
        return {"matches": rows[:limit], "more": len(rows) > limit}

    def player_of_match(self, player: str | None = None, season: int | None = None, limit: int = 5) -> dict:
        if player:
            rows = self.q("SELECT season, date, team1, team2 FROM matches WHERE player_of_match = ? ORDER BY date", (player,))
            return {"player": player, "awards": len(rows), "by_season": self.q(
                "SELECT season, COUNT(*) AS awards FROM matches WHERE player_of_match = ? GROUP BY season ORDER BY season", (player,))}
        rows = self.q(f"""SELECT player_of_match AS player, COUNT(*) AS awards FROM matches
            WHERE player_of_match != '' {'AND season = ?' if season else ''} GROUP BY player_of_match ORDER BY awards DESC LIMIT ?""",
                      (season, limit) if season else (limit,))
        return {"season": season or "2008-2017", "leaders": rows}

    PHASES = {"powerplay": (1, 6), "middle": (7, 15), "death": (16, 20)}

    def phase_stats(self, player: str, phase: str, role: str) -> dict:
        lo, hi = self.PHASES[phase]
        if role == "bowling":
            r = self.q(f"""SELECT COUNT(DISTINCT match_id) AS matches,
                    SUM(CASE WHEN wide_runs = 0 AND noball_runs = 0 THEN 1 ELSE 0 END) AS balls,
                    SUM(total_runs - bye_runs - legbye_runs) AS runs,
                    SUM(CASE WHEN player_dismissed != '' AND dismissal_kind NOT IN ({','.join('?' * len(NOT_BOWLER_WICKETS))})
                             THEN 1 ELSE 0 END) AS wickets
                FROM deliveries WHERE bowler = ? AND over BETWEEN ? AND ? AND is_super_over = 0""",
                       (*NOT_BOWLER_WICKETS, player, lo, hi))[0]
            balls = r["balls"] or 0
            return {"player": player, "role": "bowling", "phase": f"{phase} (overs {lo}-{hi})", "matches": r["matches"],
                    "balls": balls, "runs_conceded": r["runs"] or 0, "wickets": r["wickets"] or 0,
                    "economy": round(6 * (r["runs"] or 0) / balls, 2) if balls else None}
        r = self.q("""SELECT COUNT(DISTINCT match_id) AS matches, SUM(batsman_runs) AS runs,
                SUM(CASE WHEN wide_runs = 0 THEN 1 ELSE 0 END) AS balls,
                SUM(CASE WHEN player_dismissed = batsman THEN 1 ELSE 0 END) AS outs
            FROM deliveries WHERE batsman = ? AND over BETWEEN ? AND ? AND is_super_over = 0""", (player, lo, hi))[0]
        balls = r["balls"] or 0
        return {"player": player, "role": "batting", "phase": f"{phase} (overs {lo}-{hi})", "matches": r["matches"],
                "balls": balls, "runs": r["runs"] or 0, "outs": r["outs"] or 0,
                "strike_rate": round(100 * (r["runs"] or 0) / balls, 1) if balls else None}

    def team_profile(self, team: str) -> dict:
        seasons = self.q("""SELECT season, COUNT(*) AS played, SUM(CASE WHEN winner = ? THEN 1 ELSE 0 END) AS won
            FROM matches WHERE team1 = ? OR team2 = ? GROUP BY season ORDER BY season""", (team, team, team))
        played, won = sum(s["played"] for s in seasons), sum(s["won"] for s in seasons)
        best = max(seasons, key=lambda s: (s["won"] / s["played"], s["won"])) if seasons else None
        titles = self.team_titles(team)
        top = self.q("""SELECT batsman AS player, SUM(batsman_runs) AS runs FROM deliveries WHERE batting_team = ?
            GROUP BY batsman ORDER BY runs DESC LIMIT 3""", (team,))
        summary = (f"{team} played {played} IPL matches in {len(seasons)} seasons ({seasons[0]['season']}-{seasons[-1]['season']}) "
                   f"and won {won} ({round(100 * won / played, 1)}%). Titles: "
                   f"{', '.join(map(str, titles['title_seasons'])) if titles['titles_won'] else 'none'}. "
                   f"Best season by win rate: {best['season']} ({best['won']} of {best['played']}).") if seasons else f"No matches for {team}."
        return {"summary": summary, "team": team, "played": played, "won": won, "titles_won": titles["titles_won"],
                "title_seasons": titles["title_seasons"], "finals_lost": titles["finals_lost"],
                # no season-by-season table: small models misread "played 14" as "14 wins"; team_season_record covers one season
                "top_run_scorers_for_team": top}
