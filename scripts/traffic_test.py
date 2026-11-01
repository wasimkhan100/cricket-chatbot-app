#!/usr/bin/env python3
"""Run the built-in traffic tests from the terminal and print each request.

Usage: python3 scripts/traffic_test.py [rate_limit|throttle|load_shed|all]
Reads the admin token from backend/.env. The same tests are on the Dashboard tab.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
ENV = pathlib.Path(__file__).resolve().parents[1] / "backend" / ".env"
ADMIN = next(l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines() if l.startswith("CRICKET_ADMIN_TOKEN="))


def run(name: str) -> bool:
    print(f"\n=== {name} ===")
    req = urllib.request.Request(f"{BASE}/api/admin/selftest/{name}", method="POST", headers={"X-Admin-Token": ADMIN})
    passed = False
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if not line.startswith(b"data: "):
                continue
            e = json.loads(line[6:])
            if e["type"] == "step":
                print(f"  > {e['text']}")
            elif e["type"] == "request":
                result = f"429 ({e['scope']}, retry {e['retry_after']}s)" if e["status"] == 429 else (e["path"] or e["status"])
                print(f"    #{e['i']:<3} {str(result):<34} {e['ms']:>6} ms")
            elif e["type"] == "verdict":
                passed = e["passed"]
                print(f"  {'PASS' if passed else 'FAIL'}: {e['summary']}")
    return passed


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = ["rate_limit", "throttle", "load_shed"] if which == "all" else [which]
    results = {n: run(n) for n in names}
    print("\n" + "  ".join(f"{n}: {'PASS' if ok else 'FAIL'}" for n, ok in results.items()))
    sys.exit(0 if all(results.values()) else 1)
