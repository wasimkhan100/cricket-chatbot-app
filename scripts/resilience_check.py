#!/usr/bin/env python3
"""End-to-end resilience check against the running app: failure injection, rate limiting, load shedding.

Usage: python3 scripts/resilience_check.py      (reads the admin token from backend/.env)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import pathlib
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ENV = pathlib.Path(__file__).resolve().parents[1] / "backend" / ".env"
ADMIN = next(l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines() if l.startswith("CRICKET_ADMIN_TOKEN="))
results: list[tuple[str, bool, str]] = []


def req(method, path, body=None, headers=None, timeout=300):
    r = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json", **(headers or {})})
    return urllib.request.urlopen(r, timeout=timeout)


def session():
    return json.load(req("POST", "/api/session", {}))["token"]


def ask(token, q):
    try:
        with req("POST", "/api/chat", {"question": q}, {"Authorization": f"Bearer {token}"}) as r:
            done, tools = None, []
            for line in r:
                if line.startswith(b"data: "):
                    e = json.loads(line[6:])
                    if e["type"] == "tool":
                        tools.append((e["name"], e["ok"]))
                    if e["type"] == "done":
                        done = e
            return {"status": 200, "path": done["path"], "answer": done["answer"], "tools": tools}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "path": None, "answer": e.read().decode()[:120], "tools": []}


def admin(path, body):
    return json.load(req("POST", f"/api/admin/{path}", body, {"X-Admin-Token": ADMIN}))


def status():
    return json.load(req("GET", "/api/ops/status"))


def check(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def chaos(target, on):
    admin("chaos", {"target": target, "enabled": on})


def main():
    t = session()
    admin("cache/clear", {})

    # 1. Chat model down -> L2, answers still come from data.
    chaos("ollama_chat", True)
    lvl = status()["degrade_level"]
    r = ask(t, "Explain why the powerplay matters in T20")
    check("LLM down degrades, still answers", lvl == 2 and r["path"] == "degraded" and len(r["answer"]) > 40,
          f"level={lvl} path={r['path']} answer={r['answer'][:90]!r}")
    r = ask(t, "what's the score?")
    check("Templates work with LLM down", r["path"] == "template", r["answer"][:80])
    chaos("ollama_chat", False)
    time.sleep(6)  # probe loop closes the breaker
    check("LLM recovers after restore", status()["degrade_level"] == 0, f"level={status()['degrade_level']}")

    # 2. Vector DB down -> agent loses knowledge search, keeps stats tools.
    chaos("chroma", True)
    r = ask(t, "How many sixes did Chris Gayle hit in IPL 2012?")
    check("Chroma down: stats tools still answer", r["path"] in ("llm", "fact") and any(n == "get_player_stats" and ok for n, ok in r["tools"]),
          f"path={r['path']} tools={r['tools']} answer={r['answer'][:80]!r}")
    chaos("chroma", False)

    # 3. Stats DB down -> agent answers from notes, tool failure is contained.
    chaos("stats_db", True)
    r = ask(t, "What is the Duckworth-Lewis method?")
    check("Stats DB down: knowledge answers still work", r["path"] in ("llm", "knowledge"), f"path={r['path']} answer={r['answer'][:80]!r}")
    chaos("stats_db", False)

    # 4. Feed stalls -> state freezes, ticker keeps serving last known state.
    chaos("feed", True)
    v1 = json.load(req("GET", "/api/live"))["version"]
    time.sleep(9)
    v2 = json.load(req("GET", "/api/live"))["version"]
    check("Feed stall freezes state, API keeps serving", v1 == v2, f"version {v1} -> {v2}")
    chaos("feed", False)

    time.sleep(1)
    # 5. Rate limit per fan (burst 6, then 20/min).
    t2 = session()
    codes = [ask(t2, "what's the score?")["status"] for _ in range(10)]
    check("Per-fan rate limit returns 429", codes.count(429) >= 3 and codes[:6] == [200] * 6, f"codes={codes}")

    # 6. Load shedding: more parallel LLM questions than slots + queue.
    admin("cache/clear", {})
    # Reasoning questions: lookups are answered from the database now and never reach the LLM queue.
    qs = [f"Why did {t} do well in {y}? Explain" for t, y in zip(["CSK", "MI", "KKR", "SRH", "RR", "DC"], range(2008, 2014))] + \
         [f"Explain why the powerplay matters, idea {i}" for i in range(6)]
    pool_tokens = [session() for _ in range(3)]          # per-IP session creation is rate limited too
    tokens = [pool_tokens[i % 3] for i in range(len(qs))]
    with cf.ThreadPoolExecutor(len(qs)) as pool:
        out = list(pool.map(lambda a: ask(*a), zip(tokens, qs)))
    paths = [o["path"] or str(o["status"]) for o in out]
    check("Overload sheds instead of timing out", "shed" in paths and all(o["status"] in (200, 429) for o in out),
          f"paths={ {p: paths.count(p) for p in set(paths)} }")

    # 7. Auth and admin protection.
    try:
        req("POST", "/api/chat", {"question": "hi"})
        check("Chat requires a session", False, "no 401")
    except urllib.error.HTTPError as e:
        check("Chat requires a session", e.code == 401, f"status={e.code}")
    try:
        req("POST", "/api/admin/chaos", {"target": "feed", "enabled": True}, {"X-Admin-Token": "wrong"})
        check("Admin API rejects bad token", False, "accepted")
    except urllib.error.HTTPError as e:
        check("Admin API rejects bad token", e.code == 403, f"status={e.code}")

    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} checks passed")


if __name__ == "__main__":
    main()
