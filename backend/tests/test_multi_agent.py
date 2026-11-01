"""The Strands multi-agent path: router, specialists, commentator, and the transcript they leave."""

from cricket_chatbot.agents.router import route_by_rules
from tests.conftest import FakeLLM


def events_of(done, kind):
    return [e for e in done["events"] if e["type"] == kind]


def test_rules_router_picks_desks():
    r = route_by_rules("Can they chase this down?", live_q=True, has_stats_plan=False, entities=False, rule_hit=False)
    assert r.routes == ["live"] and r.method == "rules"
    r = route_by_rules("Why is Malinga so good at the death?", live_q=False, has_stats_plan=True, entities=True, rule_hit=False)
    assert r.routes == ["stats"]
    r = route_by_rules("What happens after a no ball?", live_q=False, has_stats_plan=False, entities=False, rule_hit=True)
    assert r.routes == ["rules"]
    r = route_by_rules("Explain how Kohli got out lbw and what lbw means", live_q=False, has_stats_plan=True, entities=True, rule_hit=False)
    assert r.routes == ["stats", "rules"]
    assert route_by_rules("Tell me something interesting", live_q=False, has_stats_plan=False, entities=False, rule_hit=False) is None


def test_deep_stats_question_goes_router_stats_commentator(chat):
    done = chat.ask("Why is Malinga so effective at the death?")
    assert done["path"] == "llm"
    route = events_of(done, "route")[0]
    assert route["routes"] == ["stats"] and route["method"] == "rules"
    tools = events_of(done, "tool")
    assert any(t["name"] == "phase_stats" and t["agent"] == "Stats analyst" and t["args"]["phase"] == "death" for t in tools)
    agents = [a["name"] for a in events_of(done, "agent")]
    assert agents == ["Stats analyst", "Commentator"]
    stats = next(a for a in events_of(done, "agent") if a["name"] == "Stats analyst")
    assert "death" in stats["output"] and "wickets" in stats["output"]   # the desk hands over raw tool results


def test_stats_analyst_llm_picks_a_tool_when_the_planner_cannot(chat):
    before = len(FakeLLM.calls)
    done = chat.ask("Tell me how Mumbai Indians have fared historically TOOLPICK")
    tools = events_of(done, "tool")
    picked = [t for t in tools if t.get("chosen_by") == "llm"]
    assert picked and picked[0]["name"] == "venue_stats" and picked[0]["agent"] == "Stats analyst"
    stats_call = next(c for c in FakeLLM.calls[before:] if "phase_stats" in c["tools"])
    assert "venue_stats" in stats_call["tools"] and "search_rules" not in stats_call["tools"]   # each agent sees only its tools
    commentator_call = FakeLLM.calls[-1]
    assert commentator_call["tools"] == []                                                       # the commentator has none


def test_router_agent_decides_when_rules_cannot(chat):
    done = chat.ask("Explain cricket captaincy strategy in general RULEROUTE")
    route = events_of(done, "route")[0]
    assert route["method"] == "llm" and route["routes"] == ["rules"]


def test_unusable_router_reply_falls_back_to_a_default(chat):
    done = chat.ask("Explain cricket captaincy strategy in general")
    route = events_of(done, "route")[0]
    assert route["method"] == "default" and route["routes"] == ["stats"]


def test_commentator_prompt_never_leaks(chat):
    done = chat.ask("Why is Malinga so effective at the death LEAKCOMM")
    assert "RULES YOU MUST FOLLOW" not in done["answer"]
    assert not any("RULES YOU MUST FOLLOW" in e.get("text", "") for e in done["events"] if e["type"] == "token")


def test_live_desk_uses_live_tools_and_marks_mocks(chat):
    done = chat.ask("Why might the weather and pitch matter for the chase now?")
    names = {t["name"] for t in events_of(done, "tool") if t.get("agent") == "Live"}
    assert {"get_live_score", "get_weather", "get_pitch_report"} <= names
    live = next(a for a in events_of(done, "agent") if a["name"] == "Live")
    assert "MOCK DATA" in live["output"]


def test_llm_down_still_answers_from_data(api, chat):
    from tests.conftest import admin
    admin(api, "chaos", {"target": "ollama_chat", "enabled": True})
    try:
        done = chat.ask("Why is Malinga so effective at the death?")
        assert done["path"] in ("degraded", "shed")
    finally:
        admin(api, "chaos", {"target": "ollama_chat", "enabled": False})


def test_transcript_shows_router_agents_and_tools(api, chat):
    chat.ask("Why is Malinga so effective at the death?")
    text = api.get("/api/transcript", headers={"Authorization": f"Bearer {chat.token}"}).json()["content"]
    assert "ROUTER (rules): stats" in text
    assert "[agent] Stats analyst" in text and "[agent] Commentator" in text
    assert "[tool] Stats analyst → phase_stats(" in text
    assert "[result] tool:phase_stats" in text


def test_live_follow_up_does_not_inherit_the_stats_desk():
    # Found in a real run: "can they still win this?" after a Kohli question went to the stats desk only.
    r = route_by_rules("What happened in the last few balls and can they still win this?", live_q=False, has_stats_plan=True,
                       entities=True, rule_hit=False, own_entities=False)
    assert r.routes == ["live"]
    r = route_by_rules("Is it better to chase at Chinnaswamy?", live_q=False, has_stats_plan=True, entities=False, rule_hit=False)
    assert r.routes == ["stats"]                       # "chase" alone is not about the live match


def test_specific_analysis_hides_the_generic_career_record(chat):
    done = chat.ask("Why is Malinga so effective at the death?")
    stats = next(a for a in events_of(done, "agent") if a["name"] == "Stats analyst")
    assert "phase_stats" in stats["output"] and "get_player_stats" not in stats["output"]   # no batting strike rate to misuse


def test_history_question_with_a_loose_live_word_goes_to_stats():
    # Found in a real run: "Is it better to chase at Chinnaswamy?" went to the live desk only.
    r = route_by_rules("Is it better to chase at Chinnaswamy?", live_q=True, has_stats_plan=True, entities=False,
                       rule_hit=False, own_entities=False, own_analysis=True)
    assert r.routes == ["stats"]


def test_an_answer_with_no_verifiable_sentence_is_replaced(chat):
    # Found in a real run: a failed tool call, then "6 titles, 18 finals, 21,368 runs" shown with only a warning badge.
    done = chat.ask("Why are Mumbai Indians so dominant ALLBAD")
    assert "77777" not in done["answer"] and "88888" not in done["answer"]
    assert "don't have verified figures" in done["answer"] and done["removed_sentences"] == 2


def test_team_history_is_planned_not_guessed(api):
    planner = api.app_state.agent.planner
    assert planner.analysis_plan("Tell me about Mumbai Indians' history in the IPL") == [("team_profile", {"team": "Mumbai Indians"})]


def test_invented_decimals_are_removed(chat):
    # Found in the browser check: "a strike rate of 12.56" passed because 12.56 was read as the small count 12.
    done = chat.ask("Why is Malinga so effective at the death BADDECIMAL")
    assert "12.56" not in done["answer"] and done["removed_sentences"] == 1
    assert done["answer"].startswith("Malinga is deadly")


def test_planner_fetches_every_year_and_avoids_both_roles(api):
    planner = api.app_state.agent.planner
    calls = planner.plan("Explain how Chennai Super Kings did in IPL 2010 and 2011")
    assert ("team_season_record", {"team": "Chennai Super Kings", "season": 2010}) in calls
    assert ("team_season_record", {"team": "Chennai Super Kings", "season": 2011}) in calls
    assert planner.plan("Why is Malinga so effective in the IPL?") == [
        ("get_player_stats", {"player": "SL Malinga", "role": "bowling"})]   # not "both": no batting strike rate to misuse


def test_history_answer_gets_no_live_snapshot(chat):
    from tests.conftest import FakeLLM
    chat.ask("Why is Malinga so effective at the death?")
    commentator_prompt = FakeLLM.calls[-1]["question"]
    assert "MATCH SNAPSHOT" not in commentator_prompt


def test_comparisons_are_worked_out_in_code():
    import json
    from cricket_chatbot.agents.orchestrator import compare_players
    ev = [{"source": "tool:get_player_stats", "text": json.dumps({"player": "V Kohli", "runs": 4423, "strike_rate": 129.9})},
          {"source": "tool:get_player_stats", "text": json.dumps({"player": "RG Sharma", "runs": 4207, "strike_rate": 130.9})}]
    out = compare_players(ev)
    assert "so V Kohli has the more runs" in out and "so RG Sharma has the higher strike rate" in out


def test_flipped_comparison_sentence_is_detected():
    from cricket_chatbot.agents.orchestrator import contradicts_comparison
    ctx = ("COMPARISON (worked out by the system, use as stated): V Kohli vs RG Sharma\n"
           "- runs: V Kohli 4423 vs RG Sharma 4207, so V Kohli has the more runs\n"
           "- strike rate: V Kohli 129.9 vs RG Sharma 130.9, so RG Sharma has the higher strike rate")
    assert contradicts_comparison("Kohli also has a better strike rate of 129.9 compared to Sharma's 130.9.", ctx)
    assert contradicts_comparison("Sharma has scored more runs overall.", ctx)
    assert not contradicts_comparison("Sharma has a higher strike rate (130.9 vs 129.9).", ctx)
    assert not contradicts_comparison("Kohli has scored more runs (4423) than Rohit Sharma (4207).", ctx)
    assert not contradicts_comparison("Both are fine batsmen.", ctx)


# --- fixes from the agent-flow run and the security review (2026-10-20) ---------------------------------------------

def test_number_attached_to_the_wrong_label_is_removed(chat):
    # Seen live: "84 sixes and 38 fours" passed because 84 and 38 are both in Kohli's 2016 record (84 fours, 38 sixes).
    done = chat.ask("Why was Virat Kohli so good in IPL 2016 SWAPLABEL")
    assert "84 sixes" not in done["answer"] and done["removed_sentences"] == 1
    assert done["answer"].startswith("Kohli was brilliant")
    done = chat.ask("Why was Virat Kohli so good in IPL 2016 RIGHTLABEL")
    assert "84 fours and 38 sixes" in done["answer"] and done["removed_sentences"] == 0


def test_label_check_unit():
    from cricket_chatbot.agent.agent import _label_pairs, _mislabelled
    known = _label_pairs('{"player": "V Kohli", "season": 2016, "fours": 84, "sixes": 38}')
    assert known == {(84, "fours"), (38, "sixes")}
    assert _mislabelled("He hit 84 sixes and 38 fours.", known) == {84, 38}
    assert _mislabelled("More fours (84) and sixes (38).", known) == set()
    assert _mislabelled("He hit 5 centuries.", known) == set()   # a label the data never mentions: left to the number check
    assert _mislabelled("In 2016 four times he passed fifty.", known) == set()


def test_answer_does_not_go_along_with_a_chase_that_is_not_happening(chat):
    snap = chat.c.app_state.agent.d.feed.snapshot()
    assert snap.chase() is None   # the replay hasn't reached a second innings in tests
    done = chat.ask("Why is the required rate rising CHASEPREMISE")
    assert "required run rate" not in done["answer"] and done["removed_sentences"] == 1
    assert "NOTE:" in FakeLLM.calls[-1]["question"]   # the Commentator was told there is no chase


def test_retrieved_rule_doc_does_not_add_the_rules_desk_to_a_live_question():
    r = route_by_rules("Why is the required rate rising?", live_q=True, has_stats_plan=False, entities=False, rule_hit=True)
    assert r.routes == ["live"]
    r = route_by_rules("tell me about that thing umpires do", live_q=False, has_stats_plan=False, entities=False, rule_hit=True)
    assert r.routes == ["rules"]


def test_live_question_that_also_asks_for_history_gets_both_desks(chat):
    r = route_by_rules("Can they still win this chase, and how did these two teams do head to head?", live_q=True,
                       has_stats_plan=True, entities=True, rule_hit=False, own_entities=False)
    assert r.routes == ["live", "stats"]
    r = route_by_rules("can they still win this?", live_q=True, has_stats_plan=True, entities=True, rule_hit=False,
                       own_entities=False)
    assert r.routes == ["live"]   # a live follow-up still doesn't inherit the stats desk
    done = chat.ask("Can the batting side still win this chase, and how did these two teams do head to head in IPL history?")
    assert done["path"] == "llm"   # not the one-line head-to-head fact, which ignored the live half
    route = next(e for e in done["events"] if e["type"] == "route")
    assert "live" in route["routes"] and "stats" in route["routes"]


def test_planner_fetches_both_seasons_of_a_comparison(api):
    calls = api.app_state.agent.planner.plan("Why is Virat Kohli's 2016 season special compared to his 2015 season?")
    seasons = {a.get("season") for n, a in calls if n == "get_player_stats"}
    assert seasons == {2015, 2016}


def test_one_player_over_two_seasons_is_compared_by_season():
    import json
    from cricket_chatbot.agents.orchestrator import compare_players
    ev = [{"source": "tool:get_player_stats", "text": json.dumps({"player": "V Kohli", "season": 2016, "runs": 973, "sixes": 38})},
          {"source": "tool:get_player_stats", "text": json.dumps({"player": "V Kohli", "season": 2015, "runs": 505, "sixes": 23})}]
    out = compare_players(ev)
    assert "V Kohli 2016 vs V Kohli 2015" in out and "so V Kohli 2016 has the more runs" in out
    ev += [{"source": "tool:get_player_stats", "text": json.dumps({"player": "RG Sharma", "season": 2016, "runs": 489})},
           {"source": "tool:get_player_stats", "text": json.dumps({"player": "RG Sharma", "season": 2015, "runs": 482})}]
    assert compare_players(ev) == ""   # two players over two seasons: no single side-by-side


def test_answers_shaped_by_one_fans_history_never_enter_the_shared_cache(chat, api, monkeypatch):
    # Security review SR-20261020-001: a fan's earlier turns reached other fans through the shared answer cache.
    from tests.conftest import Chat
    cache = api.app_state.agent.d.cache
    puts = []
    monkeypatch.setattr(cache, "put", lambda key, value: puts.append(key))
    fresh = Chat(api)
    fresh.ask("Why is Malinga so effective at the death?")
    assert len(puts) == 1   # no history: the answer is the same for every fan
    talker = Chat(api)
    talker.ask("Explain the LBW rule simply")
    talker.ask("Why is Malinga so effective at the death?")
    assert puts.count(puts[0]) == 1   # the repeat was written with this fan's history in the prompt: not shared


def test_season_comparison_sentence_is_not_mistaken_for_a_flip():
    # Seen live: "runs (973 in 2016 vs 505 in 2015), higher batting average (81.08 in 2016 vs 45.91 in 2015)" was removed:
    # both seasons sit before "higher", so "nearest name wins" can't tell who the comparative is about.
    from cricket_chatbot.agents.orchestrator import contradicts_comparison
    ctx = ("COMPARISON (worked out by the system, use as stated): V Kohli 2016 vs V Kohli 2015\n"
           "- average: V Kohli 2016 81.08 vs V Kohli 2015 45.91, so V Kohli 2016 has the higher batting average")
    assert not contradicts_comparison("Runs rose (973 in 2016 vs 505 in 2015), with a higher batting average "
                                      "(81.08 in 2016 vs 45.91 in 2015).", ctx)


def test_this_chase_counts_as_a_chase_claim():
    from cricket_chatbot.agent.agent import CHASE_CLAIM, NEGATION
    s = "The batting side can still win this chase."
    assert CHASE_CLAIM.search(s) and not NEGATION.search(s)
    assert CHASE_CLAIM.search("Can the batting side still win this chase?")   # the question gets the NOTE
