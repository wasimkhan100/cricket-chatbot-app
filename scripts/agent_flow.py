#!/usr/bin/env python3
"""Show the multi-agent flow for each question: the route the Router picked, each agent that ran,
every tool call (agent, arguments, ok, time) and the final answer path.

Usage: python3 scripts/agent_flow.py ["question" ...]
       CRICKBOT_URL=http://127.0.0.1:8080 python3 scripts/agent_flow.py "Explain the LBW rule simply"
With no questions it asks one per path: live template, stats, rules, live desk, comparison.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("CRICKBOT_URL", "http://127.0.0.1:8000")
DEFAULT_QUESTIONS = [
    "What's the score right now?",
    "Why did Virat Kohli do so well in IPL 2016?",
    "Explain the LBW rule simply",
    "Why is the required rate rising and who is bowling well?",
    "Compare Virat Kohli and Rohit Sharma in IPL 2016",
]


def post(path: str, body: dict | None = None, token: str | None = None):
    headers = {"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST", headers=headers)
    return urllib.request.urlopen(req, timeout=180)


def show(question: str, token: str) -> None:
    print(f"\n### {question}")
    for raw in post("/api/chat", {"question": question}, token):
        line = raw.decode().strip()
        if not line.startswith("data: "):
            continue
        e = json.loads(line[6:])
        t = e["type"]
        if t == "route":
            print(f"  route  -> {e.get('routes')} by {e.get('method')} ({e.get('reason')})")
        elif t == "agent":
            print(f"  agent  {e.get('agent') or e.get('name')}: {e.get('status') or e.get('note') or ''}")
        elif t == "tool":
            ms = f"{e['ms']}ms" if e.get("ms") is not None else ""
            print(f"  tool   {e.get('agent', '?'):<14} {e['name']}({json.dumps(e.get('args', {}))[:70]}) ok={e.get('ok')} {ms}")
        elif t == "done":
            print(f"  done   path={e['path']} verified={e.get('verified')} {e['latency_ms']}ms "
                  f"correlation_id={e.get('correlation_id')}")
            print(f"  answer {e['answer'][:220]}")
        elif t == "error":
            print(f"  ERROR  {e.get('message')} ({e.get('detail')})")


def main() -> None:
    token = json.load(post("/api/session"))["token"]
    for q in sys.argv[1:] or DEFAULT_QUESTIONS:
        show(q, token)


if __name__ == "__main__":
    main()
