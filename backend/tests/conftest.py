"""Shared fixtures. `api` runs the whole app in-process with a fake LLM: no Ollama needed, fully deterministic."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from cricket_chatbot.agent.agent import SYSTEM_PROMPT
from cricket_chatbot.config import Settings, get_settings
from cricket_chatbot.data.stats_db import build_database
from cricket_chatbot.llm.ollama import LLMUnavailable, OllamaClient
from cricket_chatbot.main import create_app

ADMIN = "test-admin-token-0123456789"


class FakeLLM:
    """Answers by keyword in the fan's question, so tests can force each behaviour."""

    calls: list[dict] = []
    small_stat = 13  # SCORESTAT answers use it; tests set it to a number that is on the live scorecard

    @staticmethod
    async def chat_stream(self, *, model, messages, tools=None, think=False, options=None, keep_alive="30m"):
        self.health.check_chaos("ollama_chat") if "ollama_chat" in self.health.chaos else None
        if "ollama_chat" in self.health.chaos:
            raise LLMUnavailable("chaos")
        q = messages[-1]["content"]
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        FakeLLM.calls.append({"model": model, "question": q, "system": system[:40], "tools": [t["function"]["name"] for t in tools or []]})
        if system.startswith("You route"):  # the Router agent
            text = '{"routes": ["rules"], "reason": "fake router"}' if "RULEROUTE" in q else "not json at all"
            yield {"message": {"content": text}, "done": False}
            yield {"message": {}, "done": True, "eval_count": 5, "prompt_eval_count": 50}
            return
        if tools and "TOOLPICK" in q and not any(m["role"] == "tool" for m in messages):  # the Stats analyst picks a tool
            yield {"message": {"content": "", "tool_calls": [{"function": {"name": "venue_stats", "arguments": {"venue": "Wankhede"}}}]},
                   "done": False}
            yield {"message": {}, "done": True, "eval_count": 9, "prompt_eval_count": 80}
            return
        if "LLMFAIL" in q and not tools:  # the model dies mid-answer (the Commentator's call)
            yield {"message": {"content": "Starting "}, "done": False}
            raise LLMUnavailable("simulated failure mid-stream")
        if "LEAKME" in q:
            text = "Sure! " + SYSTEM_PROMPT  # a model talked into printing its instructions
        elif "LEAKCOMM" in q:
            from cricket_chatbot.agents.orchestrator import COMMENTATOR_PROMPT
            text = "Fine. " + COMMENTATOR_PROMPT
        elif "SCORESTAT" in q:
            text = f"Chennai Super Kings were champions that year. In 2011 they played {FakeLLM.small_stat} matches."
        elif "SMALLSTAT" in q:
            text = "Teams batting first often do well here. One side played 13 matches and won 3 of them."
        elif "BADDECIMAL" in q:
            text = "Malinga is deadly at the death. His strike rate there is 12.56 balls per wicket."
        elif "SWAPLABEL" in q:  # right numbers, wrong labels (the data has 84 fours and 38 sixes)
            text = "Kohli was brilliant that season. He hit 84 sixes and 38 fours."
        elif "RIGHTLABEL" in q:
            text = "Kohli was brilliant that season. He hit 84 fours and 38 sixes."
        elif "CHASEPREMISE" in q:  # goes along with a chase that isn't happening
            text = "The required run rate is climbing fast. The bowlers have been tidy."
        elif "ALLBAD" in q:
            text = "Mumbai have won 77777 titles. They also scored 88888 runs."
        elif "BADNUMBER" in q:
            text = "Kohli is a fine player. He scored 98765 runs last week."
        elif "<script" in q:
            text = "That looks like code, so here is plain text only."
        else:
            text = "Here is a short cricket answer from the fake model."
        for word in text.split(" "):
            yield {"message": {"content": word + " "}, "done": False}
        yield {"message": {}, "done": True, "eval_count": len(text.split()), "prompt_eval_count": 100}

    @staticmethod
    async def embed(self, texts, model):
        if "ollama_embed" in self.health.chaos:
            raise LLMUnavailable("chaos")
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            out.append([((h[i % 32] / 255) - 0.5) for i in range(768)])
        return out

    @staticmethod
    async def list_models(self):
        s = get_settings()
        return [s.chat_model, s.think_model, f"{s.embed_model}:latest"]


@pytest.fixture(scope="session")
def built_db(tmp_path_factory):
    var = tmp_path_factory.mktemp("var")
    build_database(get_settings().data_dir, var / "cricket.sqlite3")
    return var


@pytest.fixture(scope="module")
def api(built_db, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(OllamaClient, "chat_stream", FakeLLM.chat_stream)
    mp.setattr(OllamaClient, "embed", FakeLLM.embed)
    mp.setattr(OllamaClient, "list_models", FakeLLM.list_models)
    var = tmp_path_factory.mktemp("module-var")
    (var / "cricket.sqlite3").write_bytes((built_db / "cricket.sqlite3").read_bytes())
    settings = Settings(var_dir=var, frontend_dist=var / "no-ui", replay_autostart=False, replay_ball_interval_s=30,
                        session_secret="s" * 40, admin_token=ADMIN, rate_per_client_per_min=10_000,
                        rate_burst_per_client=1_000, rate_global_per_s=10_000, answer_cache_ttl_s=0.001, session_create_per_min=100_000)
    app = create_app(settings)
    with TestClient(app) as client:
        client.app_state = app.state
        yield client
    mp.undo()


class Chat:
    def __init__(self, client):
        self.c = client
        self.token = client.post("/api/session").json()["token"]

    def raw(self, question, **extra):
        return self.c.post("/api/chat", json={"question": question, **extra}, headers={"Authorization": f"Bearer {self.token}"})

    def ask(self, question, **extra) -> dict:
        r = self.raw(question, **extra)
        assert r.status_code == 200, r.text
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        done = next(e for e in events if e["type"] in ("done", "error"))
        done["events"] = events
        return done


@pytest.fixture
def chat(api):
    return Chat(api)


def admin(api, path, body=None, raw=None):
    h = {"X-Admin-Token": ADMIN, "Content-Type": "application/json"}
    if raw is not None:
        return api.post(f"/api/admin/{path}", content=raw, headers=h)
    return api.post(f"/api/admin/{path}", json=body or {}, headers=h)
