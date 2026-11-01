
import pytest

from cricket_chatbot.agent.memory import SingleFlight, TTLCache, normalise_question
from cricket_chatbot.agent.planner import Planner
from cricket_chatbot.agent.router import classify, template_answer
from cricket_chatbot.config import get_settings
from cricket_chatbot.data.stats_db import StatsDB, build_database
from cricket_chatbot.live.feed import LiveFeed
from cricket_chatbot.live.state import BallEvent, MatchState, StateBuilder
from cricket_chatbot.resilience.health import HealthRegistry


@pytest.fixture(scope="session")
def db(tmp_path_factory) -> StatsDB:
    path = tmp_path_factory.mktemp("db") / "cricket.sqlite3"
    build_database(get_settings().data_dir, path)
    return StatsDB(path)


def ev(seq, **kw):
    base = dict(inning=1, over=1, ball=seq, batting_team="A", bowling_team="B", batsman="X", non_striker="Y",
                bowler="Z", batsman_runs=0, extra_runs=0, total_runs=0, wide_runs=0, noball_runs=0, bye_runs=0, legbye_runs=0)
    base.update(kw)
    return BallEvent(seq=seq, **base)


def test_state_builder_scores_and_is_idempotent():
    b = StateBuilder(MatchState(1, 2017, "V", "A", "B", "toss", "A vs B"))
    assert b.apply(ev(1, batsman_runs=4, total_runs=4))
    assert b.apply(ev(2, wide_runs=1, extra_runs=1, total_runs=1))
    assert b.apply(ev(3, player_dismissed="X", dismissal_kind="bowled"))
    assert not b.apply(ev(2, batsman_runs=6, total_runs=6))   # duplicate seq ignored
    inn = b.state.current
    assert (inn.runs, inn.wickets, inn.legal_balls, inn.extras, inn.fours) == (5, 1, 2, 1, 1)
    assert inn.batters["X"].balls == 2               # wides don't count as balls faced
    assert inn.bowlers["Z"].wickets == 1 and inn.bowlers["Z"].runs == 5
    assert b.state.last_balls == ["4", "1wd", "W"]
    assert b.state.version == 3


def test_chase_maths():
    b = StateBuilder(MatchState(1, 2017, "V", "A", "B", "toss", "A vs B"))
    b.apply(ev(1, total_runs=6, batsman_runs=6))
    b.apply(ev(2, inning=2, batting_team="B", bowling_team="A", total_runs=1, batsman_runs=1))
    c = b.state.chase()
    assert c == {"target": 7, "runs_needed": 6, "balls_left": 119, "required_run_rate": round(36 / 119, 2)}


def test_replay_of_2017_final_matches_the_scorecard(db):
    feed = LiveFeed(db, 59, 1.0, HealthRegistry())
    while feed.apply_next():
        pass
    s = feed.state
    assert s.status == "complete"
    assert [(i.runs, i.wickets) for i in s.innings] == [(129, 8), (128, 6)]
    assert s.result.startswith("Mumbai Indians won by 1 run")


def test_stats_queries(db):
    assert db.resolve_player("kohli")[0] == "V Kohli"
    assert db.resolve_player("Sachin") == ["SR Tendulkar"]
    assert db.resolve_team("csk") == "Chennai Super Kings"
    assert db.batting("V Kohli", 2016)["runs"] == 973
    assert db.season_summary(2010)["champion"] == "Chennai Super Kings"
    assert db.head_to_head("Mumbai Indians", "Chennai Super Kings")["matches"] == 22


def test_stats_db_is_read_only(db):
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        db.q("DELETE FROM matches")


def test_router_templates_only_simple_live_questions():
    assert classify("what's the score?") == "score"
    assert classify("who won the toss") == "toss"
    assert classify("who won IPL 2016?") is None         # history -> agent
    assert classify("why did they lose that wicket?") is None
    s = MatchState(1, 2017, "V", "A", "B", "A won the toss and chose to bat", "A vs B")
    assert "hasn't started" in template_answer("score", s)


def test_planner(db):
    p = Planner(db)
    assert p.plan("Who won IPL 2010?") == [("season_summary", {"season": 2010})]
    assert p.plan("csk vs rcb record")[0][0] == "head_to_head"
    assert p.plan("Career bowling stats of JJ Bumrah?") == [("get_player_stats", {"player": "JJ Bumrah", "role": "bowling"})]
    assert p.plan("Is Smith batting well?", live_players=("SPD Smith",))[0][1]["player"] == "SPD Smith"
    assert p.plan("What is a free hit?") == []


def test_cache_and_normalisation():
    assert normalise_question("Hey, what's the SCORE please??") == normalise_question("what s the score")
    c = TTLCache(2, ttl_s=60)
    c.put("a", 1); c.put("b", 2); c.put("c", 3)
    assert c.get("a") is None and c.get("c") == 3


async def test_single_flight_runs_once():
    import asyncio
    sf, calls = SingleFlight(), 0

    async def work():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "answer"

    results = await asyncio.gather(*(sf.run("k", work) for _ in range(10)))
    assert results == ["answer"] * 10 and calls == 1


def test_follow_up_detection():
    from cricket_chatbot.agent.memory import is_follow_up
    h = [{"role": "user", "content": "Who won IPL 2010?"}]
    assert is_follow_up("and who was the top scorer that season?", h)
    assert is_follow_up("what about his strike rate", h)
    assert not is_follow_up("What is the difference between a bye and a leg bye?", h)
    assert not is_follow_up("and his strike rate?", [])


def test_follow_up_keeps_subject_not_season(db):
    p = Planner(db)
    prev = "How many runs did Virat Kohli score in IPL 2016?"
    assert p.plan("How many IPL runs has he scored in his career?", context_q=prev) == \
        [("get_player_stats", {"player": "V Kohli", "role": "batting"})]
    assert p.plan("and how many sixes that season?", context_q=prev)[0][1]["season"] == 2016


def test_explanations_skip_templates():
    assert classify("How is the target revised when rain shortens a match?") is None
    assert classify("what's the target?") == "chase"


def test_titles_and_whole_word_names(db):
    p = Planner(db)
    assert p.plan("How many runs did Virat Kohli score in IPL 2016?") == \
        [("get_player_stats", {"player": "V Kohli", "role": "batting", "season": 2016})]
    assert p.plan("Did Deccan Chargers ever win the IPL?") == [("team_titles", {"team": "Deccan Chargers"})]
    t = db.team_titles("Chennai Super Kings")
    assert t["title_seasons"] == [2010, 2011] and t["finals_lost"] == [2008, 2012, 2013, 2015]
    assert db.team_titles("Deccan Chargers")["title_seasons"] == [2009]
