"""Agent-level observability: runs, correlation, tokens, tools, handoffs, errors, CSVs, redaction, no side effects."""

import csv
import io

from cricket_chatbot.observability.agent_runs import observer
from tests.conftest import ADMIN, Chat, admin

H = {"X-Admin-Token": ADMIN}


def workflow(api, correlation_id):
    r = api.get(f"/api/admin/agents/workflow/{correlation_id}", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_multi_agent_turn_is_recorded_end_to_end(api, chat):
    done = chat.ask("Why is Malinga so effective at the death?")
    wf = workflow(api, done["correlation_id"])
    runs = {r["agent"]: r for r in wf["runs"]}
    assert {"Crickbot", "Planner", "Router", "Stats analyst", "Commentator"} <= set(runs)
    assert len({r["trace_id"] for r in wf["runs"]}) == 1 and runs["Crickbot"]["trace_id"]       # one trace for the whole turn
    root = runs["Crickbot"]["run_id"]
    assert all(r["parent_run_id"] == root for r in wf["runs"] if r["agent"] != "Crickbot")      # everything hangs off the turn
    assert runs["Router"]["handoff_from"] == "Planner" and runs["Stats analyst"]["handoff_from"] == "Router"
    assert runs["Commentator"]["handoff_from"] == "Stats analyst"
    assert any(t["tool"] == "phase_stats" and t["ok"] for t in runs["Stats analyst"]["tool_calls"])
    assert runs["Commentator"]["llm_calls"] >= 1 and runs["Commentator"]["tokens_out"] > 0 and runs["Commentator"]["models"]
    assert runs["Router"]["llm_calls"] == 0                                                     # decided by rules, no LLM
    assert runs["Commentator"]["output_summary"] and runs["Crickbot"]["output_summary"].startswith("[llm]")
    assert all(r["status"] == "ok" and r["latency_ms"] >= 0 for r in wf["runs"])


def test_fast_path_turn_only_runs_the_planner(chat, api):
    done = chat.ask("Who won IPL 2010?")
    agents = [r["agent"] for r in workflow(api, done["correlation_id"])["runs"]]
    assert agents == ["Crickbot", "Planner"]


def test_llm_failure_inside_an_agent_is_tracked(api, chat):
    done = chat.ask("Why is Malinga so effective at the death LLMFAIL")
    assert done["path"] == "degraded"                       # behaviour unchanged: the fan still gets a data answer
    runs = {r["agent"]: r for r in workflow(api, done["correlation_id"])["runs"]}
    assert runs["Commentator"]["status"] == "error" and "LLMUnavailable" in runs["Commentator"]["error"]
    assert runs["Commentator"]["llm_calls"] == 1             # the failed call is counted
    assert runs["Crickbot"]["status"] == "degraded"
    errors = api.get("/api/admin/agents/runs?status=error&agent=Commentator", headers=H).json()["runs"]
    assert any(r["correlation_id"] == done["correlation_id"] for r in errors)


def test_dashboard_break_degrades_before_any_agent_runs(api, chat):
    admin(api, "chaos", {"target": "ollama_chat", "enabled": True})
    try:
        done = chat.ask("Why is Malinga so effective at the death?")
    finally:
        admin(api, "chaos", {"target": "ollama_chat", "enabled": False})
    agents = [r["agent"] for r in workflow(api, done["correlation_id"])["runs"]]
    assert "Commentator" not in agents and done["path"] == "degraded"


def test_filters_by_agent_and_run_id(api, chat):
    done = chat.ask("Why is Malinga so effective at the death?")
    commentator = next(r for r in workflow(api, done["correlation_id"])["runs"] if r["agent"] == "Commentator")
    hits = api.get(f"/api/admin/agents/runs?q={commentator['run_id']}", headers=H).json()["runs"]
    assert [r["run_id"] for r in hits] == [commentator["run_id"]]
    only = api.get("/api/admin/agents/runs?agent=Router&limit=5", headers=H).json()["runs"]
    assert only and all(r["agent"] == "Router" for r in only)
    summary = api.get("/api/admin/agents/summary", headers=H).json()
    names = {a["agent"] for a in summary["agents"]}
    assert {"Crickbot", "Planner", "Router", "Commentator"} <= names
    assert any(h["handoff"] == "Planner → Router" for h in summary["handoffs"])


def test_dashboard_endpoints_need_the_admin_token(api):
    for path in ("summary", "runs", "csv", "workflow/x", "csv/agent_runs_2026-01-01.csv"):
        assert api.get(f"/api/admin/agents/{path}").status_code in (401, 403)


def test_csv_files_are_written_and_downloadable(api, chat):
    chat.ask("Why is Malinga so effective at the death?")
    observer.write_metrics_snapshot()
    files = {f["name"].split("_20")[0]: f["name"] for f in api.get("/api/admin/agents/csv", headers=H).json()["files"]}
    assert {"agent_runs", "tool_calls", "agent_metrics"} <= set(files)
    r = api.get(f"/api/admin/agents/csv/{files['agent_runs']}", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows and {"run_id", "correlation_id", "trace_id", "agent", "status", "latency_ms", "tokens_in", "tokens_out",
                     "tool_calls", "retries", "handoff_from"} <= set(rows[0])
    assert any(row["agent"] == "Commentator" for row in rows)
    tools = list(csv.DictReader(io.StringIO(api.get(f"/api/admin/agents/csv/{files['tool_calls']}", headers=H).text)))
    assert any(t["tool"] == "phase_stats" for t in tools)
    metrics = list(csv.DictReader(io.StringIO(api.get(f"/api/admin/agents/csv/{files['agent_metrics']}", headers=H).text)))
    assert any(m["agent"] == "Commentator" and float(m["success_rate"]) >= 0 for m in metrics)
    for bad in ("../.env", "agent_runs.csv", "x_2026-01-01.csv", "..%2F..%2Fcricket.sqlite3"):
        assert api.get(f"/api/admin/agents/csv/{bad}", headers=H).status_code == 404


def test_secrets_never_reach_runs_or_csv(api):
    c = Chat(api)
    secret = api.app_state.settings.admin_token
    done = c.ask(f"password=hunter22 my admin token is {secret} why is Malinga so good at the death?")
    runs = workflow(api, done["correlation_id"])["runs"]
    blob = str(runs)
    assert "hunter22" not in blob and secret not in blob
    files = api.get("/api/admin/agents/csv", headers=H).json()["files"]
    for f in files:
        text = api.get(f"/api/admin/agents/csv/{f['name']}", headers=H).text
        assert secret not in text and "hunter22" not in text


def test_a_broken_recorder_never_breaks_an_answer(api, chat, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(observer, "_write_csv", boom)
    done = chat.ask("Why is Malinga so effective at the death?")
    assert done["type"] == "done" and done["path"] == "llm" and done["answer"]


def test_prometheus_has_agent_metrics(api, chat):
    chat.ask("Why is Malinga so effective at the death?")
    text = api.get("/metrics").text
    for name in ("agent_runs_total", "agent_run_seconds", "agent_tokens_total", "agent_run_tool_calls_total", "agent_handoffs_total"):
        assert name in text
    assert 'agent="Commentator"' in text


# --- dashboard data endpoints ---------------------------------------------------------------
def test_timeseries_tools_errors_and_run_detail_come_from_real_runs(api, chat):
    chat.ask("Why is Malinga so effective at the death?")
    failed = chat.ask("Why is Malinga so effective at the death LLMFAIL")
    ts = api.get("/api/admin/agents/timeseries?range=15m", headers=H).json()
    assert ts["bucket_s"] == 30 and sum(p["ok"] + p["error"] + p["other"] for p in ts["points"]) > 0
    assert sum(p["tokens_out"] for p in ts["points"]) > 0 and sum(p["error"] for p in ts["points"]) >= 1
    tools = {t["tool"]: t for t in api.get("/api/admin/agents/tools?range=15m", headers=H).json()["tools"]}
    assert tools["phase_stats"]["calls"] >= 1 and "Stats analyst" in tools["phase_stats"]["agents"]
    errors = api.get("/api/admin/agents/errors?range=15m", headers=H).json()["errors"]
    g = next(e for e in errors if e["agent"] == "Commentator" and e["kind"] == "run")
    assert "LLMUnavailable" in g["message"] and g["count"] >= 1
    run = api.get(f"/api/admin/agents/runs/{g['last_run_id']}", headers=H).json()
    assert run["status"] == "error" and run["correlation_id"] in (g["last_correlation_id"], failed["correlation_id"])
    assert api.get("/api/admin/agents/runs/nope", headers=H).status_code == 404
    s = api.get("/api/admin/agents/summary?range=15m", headers=H).json()
    assert s["totals"]["turns"] >= 2 and s["totals"]["errors"] >= 1 and s["totals"]["cost_usd"] == 0   # local model: no price set
    assert {a["health"] for a in s["agents"]} <= {"healthy", "degraded", "failing", "idle"}
    assert api.get("/api/admin/agents/summary?range=2d", headers=H).status_code == 422


def test_logs_are_searchable_by_correlation_id(api, chat):
    done = chat.ask("Why is Malinga so effective at the death?")
    r = api.get(f"/api/admin/agents/logs?correlation_id={done['correlation_id']}&logger=agent_runs", headers=H).json()
    agents = {line["agent"] for line in r["lines"]}
    assert {"Crickbot", "Planner", "Router", "Commentator"} <= agents
    assert all(line["msg"] == "agent run" for line in r["lines"])
    warn = api.get("/api/admin/agents/logs?level=WARNING&limit=50", headers=H).json()["lines"]
    assert all(line["level"] in ("WARNING", "ERROR", "CRITICAL") for line in warn)


def test_history_survives_a_restart_and_cost_uses_configured_prices(tmp_path):
    from cricket_chatbot.observability.agent_runs import AgentObserver
    first = AgentObserver()
    first.configure(tmp_path)
    with first.run("Commentator", "q") as r:
        r.tokens_in, r.tokens_out = 1_000_000, 500_000
        first.note_tool("phase_stats", {"player": "x"}, True, 3.0)
    second = AgentObserver()
    second.configure(tmp_path, prices=(0.10, 0.50))           # e.g. a hosted model at $0.10 / $0.50 per MTok
    runs = second.query()
    assert second.history_loaded == 1 and runs[0]["run_id"] == r.run_id and runs[0]["tool_calls"][0]["tool"] == "phase_stats"
    assert runs[0]["cost_usd"] == 0.35 and second.summary()["totals"]["cost_usd"] == 0.35


def test_request_ids_are_not_mistaken_for_phone_numbers():
    # Found on the dashboard: request id "5b8765432109edd5" was logged as "5b[phone]edd5".
    from cricket_chatbot.observability.logging import redact
    assert redact("5b8765432109edd5") == "5b8765432109edd5"
    assert redact("call +91 98765 43210 or 9876543210") == "call [phone] or [phone]"


def test_logs_hide_dashboard_polling_by_default(api, chat):
    chat.ask("Who won IPL 2010?")
    api.get("/api/admin/agents/summary", headers=H)
    paths = [line.get("path", "") for line in api.get("/api/admin/agents/logs?logger=http", headers=H).json()["lines"]]
    assert "/api/chat" in paths and not any(p.startswith("/api/admin/") for p in paths)
    shown = [line.get("path", "") for line in api.get("/api/admin/agents/logs?logger=http&hide_polling=false", headers=H).json()["lines"]]
    assert any(p.startswith("/api/admin/") for p in shown)


def test_workflow_lists_the_parent_first_on_a_timestamp_tie(tmp_path):
    from cricket_chatbot.observability.agent_runs import AgentObserver, AgentRun
    o = AgentObserver()
    child = AgentRun(agent="Planner", parent_run_id="root1", correlation_id="c1", started=100.0, ended=100.1)
    root = AgentRun(agent="Crickbot", run_id="root1", correlation_id="c1", started=100.0, ended=105.0)
    o.runs.extend([child, root])
    assert [r["agent"] for r in o.workflow("c1")] == ["Crickbot", "Planner"]


def test_streaming_agents_leave_no_opentelemetry_context_errors(api, chat, caplog):
    # Found on the dashboard: every Commentator answer logged "Failed to detach context" (ERROR) from OpenTelemetry.
    import logging
    with caplog.at_level(logging.ERROR):
        done = chat.ask("Why is Malinga so effective at the death?")
    assert done["path"] == "llm"
    assert not [r for r in caplog.records if "detach context" in r.getMessage()]
    runs = workflow(api, done["correlation_id"])["runs"]
    assert len({r["trace_id"] for r in runs}) == 1       # agent spans still join the turn's trace
