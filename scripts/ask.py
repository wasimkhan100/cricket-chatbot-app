#!/usr/bin/env python3
"""Ask the running chatbot questions from the terminal.  Usage: python3 scripts/ask.py "question" [--think]"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"


def post(path, body, token=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    return urllib.request.urlopen(req, timeout=300)


def main():
    think = "--think" in sys.argv
    questions = [a for a in sys.argv[1:] if a != "--think"]
    token = json.load(post("/api/session", {}))["token"]
    for q in questions:
        print(f"\nQ: {q}")
        with post("/api/chat", {"question": q, "think": think}, token) as r:
            for line in r:
                if not line.startswith(b"data: "):
                    continue
                e = json.loads(line[6:])
                if e["type"] == "route":
                    print(f"  router ({e['method']}) -> {', '.join(e['routes'])}  [{e.get('reason', '')}]")
                elif e["type"] == "agent":
                    print(f"  agent {e['name']}: {e['status']} ({e['ms']} ms)")
                elif e["type"] == "tool":
                    who = f"{e['agent']} -> " if e.get("agent") else ""
                    print(f"  · tool {who}{e['name']} {json.dumps(e.get('args'))[:90]} ok={e['ok']}"
                          + (" (chosen by the agent)" if e.get("chosen_by") == "llm" else ""))
                elif e["type"] == "thinking":
                    pass
                elif e["type"] == "done":
                    print(f"A: {e['answer']}")
                    print(f"  [{e['path']} · {e['latency_ms']} ms · first token {e['first_token_ms']} ms · "
                          f"verified={e['verified']} {e['unverified'] or ''} · thinking chars {e['thinking_chars']}]")
                elif e["type"] == "error":
                    print("ERROR", e)


if __name__ == "__main__":
    main()
