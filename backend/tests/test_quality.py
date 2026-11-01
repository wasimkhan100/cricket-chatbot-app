"""The evaluation metrics, and the evidence the agent reports for them."""

import pytest

from cricket_chatbot.rag import quality as q


def test_recall_and_reciprocal_rank():
    ranked = ["a", "b", "c", "d", "e"]
    assert q.recall_at_k(ranked, ["c"], 1) == 0.0
    assert q.recall_at_k(ranked, ["c"], 3) == 1.0
    assert q.recall_at_k(ranked, ["a", "z"], 5) == 0.5      # two relevant chunks, one found
    assert q.recall_at_k(ranked, [], 5) is None             # unlabelled questions don't count
    assert q.reciprocal_rank(ranked, ["c"]) == pytest.approx(1 / 3)
    assert q.reciprocal_rank(ranked, ["z"]) == 0.0


def test_context_precision_counts_used_chunks():
    assert q.context_precision(["a", "b"], ["a"]) == 0.5
    assert q.context_precision([], ["a"]) is None


def test_claims_and_numbers():
    claims = q.split_claims("Sure! Kohli scored 4423 runs. His strike rate is 129.9, which beats Rohit.")
    assert claims == ["Kohli scored 4423 runs.", "His strike rate is 129.9, which beats Rohit."]
    ev = "V Kohli: 4,423 runs, strike rate 129.9"
    assert q.unsupported_numbers("Kohli scored 4423 runs at 129.9", ev) == []
    assert q.unsupported_numbers("Kohli scored 4523 runs off 12 balls", ev) == ["4523"]   # 12 balls: a free small count
    assert q.unsupported_numbers("Kohli played 12 games", ev) == ["12"]                    # 12 games: a stat, must be in the data
    assert q.unsupported_numbers("Kohli scored 973 in 2016", ev, "runs in 2016?") == ["973"]


def test_answer_checks():
    assert q.check_answer("Chennai Super Kings won.", [["Chennai Super Kings"]], []) == []
    assert q.check_answer("Mumbai won.", [["Chennai Super Kings", "CSK"]], ["Mumbai"]) == [
        "missing one of ['Chennai Super Kings', 'CSK']", "contains 'Mumbai'"]


def test_triage_checks_retrieval_first():
    assert q.triage(retrieved=True, answer_problems=[], ungrounded=0) == "ok"
    assert q.triage(retrieved=False, answer_problems=["missing 973"], ungrounded=0) == "retrieval_miss"
    assert q.triage(retrieved=True, answer_problems=[], ungrounded=1) == "generation_error"
    assert q.retrieval_ok(["live", "tool:season_summary"], ["tool:season_summary", "doc:season-2010"])
    assert not q.retrieval_ok(["live"], ["doc:season-2010"])
    assert q.retrieval_ok(["live"], [])


def test_calibration_stats_and_trust_gate():
    perfect = [("supported", "supported")] * 5 + [("unsupported", "unsupported")] * 5
    s = q.calibration_stats(perfect)
    assert s["accuracy"] == 1.0 and s["kappa"] == 1.0 and s["trusted"]
    # A judge that says "supported" to everything agrees 50% of the time and catches nothing: never trusted.
    lazy = [("supported", "supported")] * 5 + [("unsupported", "supported")] * 5
    s = q.calibration_stats(lazy)
    assert s["accuracy"] == 0.5 and s["kappa"] == 0.0 and s["catches_unsupported"] == 0.0 and not s["trusted"]


def test_stage_latency_splits_a_trace():
    trace = {"duration_ms": 1000, "spans": [
        {"name": "chat.answer", "duration_ms": 1000}, {"name": "rag.search", "duration_ms": 60},
        {"name": "ollama.embed", "duration_ms": 40}, {"name": "tool.season_summary", "duration_ms": 10},
        {"name": "ollama.chat", "duration_ms": 800}]}
    assert q.stage_latency(trace) == {"embed": 40, "search": 20, "tools": 10, "llm": 800, "other": 130, "total": 1000}
    assert q.pct([100, 200, 300, 400], 50) == 200 and q.pct([100, 200, 300, 400], 95) == 400


def test_agent_reports_evidence_but_the_api_strips_it(api, chat):
    agent = api.app_state.agent

    async def answer():
        return [e async for e in agent.answer("Who won IPL 2010?", "eval-test")]

    done = next(e for e in api.portal.call(answer) if e["type"] == "done")
    sources = [e["source"] for e in done["evidence"]]
    assert "tool:season_summary" in sources
    assert any("Chennai Super Kings" in e["text"] for e in done["evidence"])
    assert "evidence" not in chat.ask("Who won IPL 2010?")   # internal only, never sent to browsers


def test_report_page_renders_and_escapes():
    from cricket_chatbot.rag.quality_report import render

    case = {"q": "<b>Who</b> won?", "kind": "stats", "path": "fact", "answer": "CSK <script>", "recall@1": 1.0, "recall@3": 1.0,
            "recall@5": 1.0, "rr": 1.0, "top5": ["season-2010"], "used_sources": ["tool:season_summary"], "context_precision": None,
            "retrieval_ok": True, "answer_problems": [], "claims": [{"claim": "CSK won.", "bad_numbers": [], "judge": "supported",
            "judge_reason": "stated"}], "grounded": 1.0, "verdict": "ok", "first_token_ms": 150,
            "latency": {"embed": 40, "search": 20, "tools": 5, "llm": 0, "other": 80, "total": 145}}
    rep = {"started": "now", "judge_model": "m", "cases": [case],
           "calibration": q.calibration_stats([("supported", "supported"), ("unsupported", "unsupported")]) | {
               "untouched_half": {"items": 1, "accuracy": 1.0, "kappa": 1.0, "trusted": True}, "llm_alone": {"accuracy": 1.0, "kappa": 1.0},
               "disagreements": [], "labels_reviewed_by_human": False},
           "summary": q.summarise([case], trusted=True, judged=True)}
    page = render(rep)
    assert "RAG + LLM evaluation" in page and "TRUSTED" in page and "not been reviewed" in page
    assert "<script>" not in page and "&lt;script&gt;" in page   # answers are escaped


def test_scoreboard_numbers_dont_vouch_for_history_answers(api, chat):
    """A history answer only sees a one-line live brief, so numbers elsewhere on the live scorecard must not
    whitelist an invented stat (found by the evaluation: "In 2011 they played 14 matches and won 7")."""
    from cricket_chatbot.agent.agent import _numbers
    from tests.conftest import FakeLLM

    feed = api.app_state.feed
    for _ in range(150):
        feed.apply_next()
    snap = feed.snapshot()
    tool_numbers = {16, 9, 11, 2010, 2011}  # Chennai Super Kings' 2010 and 2011 records, which the planner may fetch
    candidates = sorted(n for n in snap.known_numbers() - _numbers(snap.to_brief()) - tool_numbers if 3 <= n <= 20)
    assert candidates, "the scorecard should hold small numbers that the brief doesn't show"
    FakeLLM.small_stat = candidates[0]
    done = chat.ask("Explain how Chennai Super Kings did in IPL 2010 and 2011 SCORESTAT")  # both years whitelisted
    assert done["path"] == "llm"
    assert f"{candidates[0]} matches" not in done["answer"] and done["removed_sentences"] == 1
