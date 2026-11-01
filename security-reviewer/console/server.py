#!/usr/bin/env python3
"""Review Console: run the security review as a pipeline of agents, with a human approving every step.

    python3 security-reviewer/console/server.py        # then open the URL it prints

Stages (each one waits for "Approve & run", shows exactly what it will execute, and waits again for you to accept the
result before the next stage unlocks):
  1 Secrets scanner      gitleaks (redacted)
  2 Dependency scanner   trivy, offline
  3 Runtime tester       promptfoo corpus against http://localhost:8080
  4 Security reviewer    Claude Code sub-agent (claude -p --agent security-reviewer), guarded by hooks/guard.py
  5 Human triage         you confirm, dispute or accept each finding
  6 Release gate         the disposition rules applied to your decisions; you sign off

Standard library only. Binds to 127.0.0.1 and requires the token in the printed URL on every API call, so another web
page can't drive it. No shell: every command is an argument list.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("REVIEW_CONSOLE_PORT", "8090"))
CONSOLE = Path(__file__).resolve().parent
SR = CONSOLE.parent
ROOT = SR.parent
REPORTS = SR / "reports"
RAW = REPORTS / "raw"
TOKEN = secrets.token_urlsafe(24)
SKIP_DIRS = ["security-reviewer/vendor", "security-reviewer/reports/archive", "frontend/node_modules", "backend/.venv",
             "Cricket-data", "docs", "backend/var", "var"]
OFFLINE_ENV = {"PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_UPDATE": "1", "SEMGREP_SEND_METRICS": "off"}

REVIEW_PROMPT = """Use your security-reviewer contract. Scope: the whole repository (it is not a git repo, so there is no git diff main...HEAD).
Exclude: security-reviewer/vendor/, security-reviewer/reports/archive/, frontend/node_modules/, backend/.venv/, Cricket-data/, docs/, var/.

1. The scanners already ran in earlier stages of this console and a human accepted their output. Read the raw outputs instead of
   re-running them: security-reviewer/reports/raw/gitleaks.json, trivy.json, promptfoo.json and any promptfoo-*.json.
2. Read the agent's prompts, tool schemas, permissions, RAG and memory/cache code. Apply the skills in security-reviewer/skills/
   (agent-release-review: all seven checks).
3. If you need to reproduce something at runtime, add cases in a NEW corpus file and run it against http://localhost:8080 only.
4. Write security-reviewer/reports/findings.json (valid against findings.schema.json), findings.sarif and summary.md.
   Every finding needs evidence a human can replay. Anything you cannot reproduce: reproducible false and "unconfirmed".
   If security-reviewer/reports/archive/ has an earlier findings.json, say for each old finding whether it is fixed, open or changed.
5. Recommend PASS, WARN or BLOCK using the rules in your contract. Do not edit application code and do not fix anything.
A human will triage your findings next; you never accept, waive or downgrade them yourself."""


def corpus_files() -> list[Path]:
    main = SR / "corpus" / "promptfooconfig.yaml"
    return [main] + sorted(p for p in (SR / "corpus").glob("sr-*.yaml"))


STAGES = [
    {"id": "secrets", "agent": "Secrets scanner", "tool": "gitleaks",
     "what": "Looks for passwords, tokens and keys in every file. --redact means the secret itself is never printed or stored.",
     "commands": [["gitleaks", "dir", "--redact", "--no-banner", "--report-format", "json",
                   "--report-path", str(RAW / "gitleaks.json"), str(ROOT)]]},
    {"id": "deps", "agent": "Dependency scanner", "tool": "trivy",
     "what": "Checks every library the app uses against the known-vulnerability database already on disk. No downloads.",
     "commands": [["trivy", "fs", "--skip-db-update", "--skip-check-update", "--skip-version-check",
                   "--scanners", "vuln,secret,misconfig", *[a for d in SKIP_DIRS for a in ("--skip-dirs", str(ROOT / d))],
                   "-f", "json", "-o", str(RAW / "trivy.json"), str(ROOT)]]},
    {"id": "runtime", "agent": "Runtime tester", "tool": "promptfoo",
     "what": "Asks the running chatbot (http://localhost:8080 only) golden, edge and attack questions and checks the tool calls "
             "and data it returns, not its wording. Fault-injection cases stay NOT RUN unless you enabled them yourself.",
     "commands": None},  # built at run time from the corpus files present
    {"id": "review", "agent": "Security reviewer", "tool": "Claude Code sub-agent",
     "what": "A Claude agent reads the code and the scanner results, applies the OWASP skills and writes findings. Every tool "
             "call it makes passes the guard (hooks/guard.py); you'll see each call and each block live.",
     "commands": None},
    {"id": "triage", "agent": "You", "tool": "human triage",
     "what": "Go through each finding: confirm it, dispute it (false positive), or accept the risk with an owner and expiry date.",
     "commands": None},
    {"id": "gate", "agent": "Release gate", "tool": "disposition rules",
     "what": "Applies the BLOCK / WARN / PASS rules to the findings and your decisions. You sign off; CI policy has the final say.",
     "commands": None},
]

lock = threading.Lock()
state: dict = {s["id"]: {"status": "waiting", "log": [], "summary": None, "started": None, "ended": None, "exit": None}
               for s in STAGES}
state["secrets"]["status"] = "ready"
proc: subprocess.Popen | None = None


def stage_commands(sid: str, prompt: str | None = None) -> list[list[str]]:
    if sid == "runtime":
        cmds = []
        for f in corpus_files():
            out = RAW / ("promptfoo.json" if f.name == "promptfooconfig.yaml" else f"promptfoo-{f.stem}.json")
            cmds.append(["promptfoo", "eval", "-c", str(f), "-o", str(out), "--no-cache", "--no-write"])
        return cmds
    if sid == "review":
        return [["claude", "-p", "--agent", "security-reviewer", "--output-format", "stream-json", "--verbose",
                 "--allowedTools", "Read", "Grep", "Glob", "Bash", "Write"]]
    st = next(s for s in STAGES if s["id"] == sid)
    return st["commands"] or []


def log(sid: str, kind: str, text: str) -> None:
    with lock:
        state[sid]["log"].append({"t": round(time.time(), 1), "kind": kind, "text": text[:4000]})
        state[sid]["log"] = state[sid]["log"][-600:]


# --- summaries (what the human reviews before accepting a stage) ------------------------------------------------------
def excluded(path: str) -> bool:
    rel = os.path.relpath(path, ROOT) if os.path.isabs(path) else path
    return any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS)


def summarise_secrets() -> dict:
    hits = json.loads((RAW / "gitleaks.json").read_text() or "[]")
    rows = [{"file": os.path.relpath(h["File"], ROOT) if os.path.isabs(h["File"]) else h["File"], "line": h.get("StartLine"),
             "rule": h.get("RuleID"), "in_scope": not excluded(h["File"])} for h in hits]  # no Match/Secret fields: never shown
    return {"total": len(rows), "in_scope": sum(r["in_scope"] for r in rows), "rows": rows[:200]}


def summarise_deps() -> dict:
    data = json.loads((RAW / "trivy.json").read_text())
    vulns, secrets_, misconf = [], 0, 0
    for r in data.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            vulns.append({"target": r.get("Target"), "pkg": v.get("PkgName"), "version": v.get("InstalledVersion"),
                          "id": v.get("VulnerabilityID"), "severity": v.get("Severity"), "fixed": v.get("FixedVersion") or ""})
        secrets_ += len(r.get("Secrets") or [])
        misconf += len(r.get("Misconfigurations") or [])
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    vulns.sort(key=lambda v: order.get(v["severity"], 5))
    counts = {k: sum(v["severity"] == k for v in vulns) for k in order}
    return {"counts": counts, "secrets": secrets_, "misconfigurations": misconf, "rows": vulns[:200]}


def summarise_runtime() -> dict:
    rows = []
    for f in sorted(RAW.glob("promptfoo*.json")):
        d = json.loads(f.read_text())
        for r in d.get("results", {}).get("results", []):
            comp = (r.get("gradingResult") or {}).get("componentResults") or []
            why = "; ".join(c.get("reason", "")[:140] for c in comp if not c.get("pass"))
            meta = (r.get("testCase") or {}).get("metadata") or {}
            status = "pass" if r.get("success") else ("not run" if "NOT RUN" in why else "fail")
            rows.append({"file": f.name, "case": meta.get("case"), "suite": meta.get("suite"),
                         "title": (r.get("testCase") or {}).get("description"), "status": status, "why": why})
    return {"rows": rows, "counts": {k: sum(r["status"] == k for r in rows) for k in ("pass", "fail", "not run")}}


def summarise_review() -> dict:
    p = REPORTS / "findings.json"
    if not p.exists():
        return {"error": "the reviewer did not write reports/findings.json"}
    d = json.loads(p.read_text())
    fs = d.get("findings", [])
    return {"recommendation": d.get("recommendation"), "count": len(fs),
            "by_severity": {s: sum(f["severity"] == s for f in fs) for s in ("Critical", "High", "Medium", "Low", "Info")},
            "summary_md": (REPORTS / "summary.md").exists()}


SUMMARISE = {"secrets": summarise_secrets, "deps": summarise_deps, "runtime": summarise_runtime, "review": summarise_review}


# --- running a stage -----------------------------------------------------------------------------------------------
def describe_stream_event(sid: str, line: str) -> None:
    """Claude Code stream-json -> readable feed lines: what the agent says, each tool call, each result or guard block."""
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        log(sid, "out", line)
        return
    t = e.get("type")
    if t == "assistant":
        for c in e["message"].get("content", []):
            if c.get("type") == "text" and c["text"].strip():
                log(sid, "say", c["text"])
            elif c.get("type") == "tool_use":
                inp = c.get("input") or {}
                brief = inp.get("command") or inp.get("file_path") or inp.get("pattern") or json.dumps(inp)[:200]
                log(sid, "tool", f"{c['name']}: {brief}")
    elif t == "user":
        for c in e["message"].get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                content = c.get("content")
                text = content if isinstance(content, str) else " ".join(x.get("text", "") for x in content or [] if isinstance(x, dict))
                log(sid, "blocked" if "guard: blocked" in text else "result", text[:600])
    elif t == "result":
        log(sid, "done", f"finished ({e.get('subtype')}) in {round((e.get('duration_ms') or 0) / 1000)} s, "
                         f"{e.get('num_turns')} turns")


def run_stage(sid: str, prompt: str | None) -> None:
    global proc
    st = state[sid]
    RAW.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **OFFLINE_ENV}
    exit_code = 0
    try:
        for cmd in stage_commands(sid, prompt):
            log(sid, "cmd", " ".join(cmd))
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, text=True, stdin=subprocess.PIPE if sid == "review" else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
            if sid == "review":
                proc.stdin.write(prompt or REVIEW_PROMPT)
                proc.stdin.close()
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                if sid == "review":
                    describe_stream_event(sid, line)
                elif sid != "deps" or "%" not in line:  # skip trivy progress bars
                    log(sid, "out", line)
            code = proc.wait()
            # gitleaks exits 1 when it finds something and promptfoo 100 when a test fails: both are results, not errors
            if code not in (0, 1, 100):
                exit_code = code
            proc = None
        st["summary"] = SUMMARISE[sid]() if sid in SUMMARISE else None
        st["status"] = "review" if exit_code == 0 else "failed"
    except Exception as e:  # show it in the UI instead of a silent dead stage
        log(sid, "error", f"{type(e).__name__}: {e}")
        st["status"] = "failed"
    st["exit"], st["ended"] = exit_code, time.time()


def unlock_next(sid: str) -> None:
    ids = [s["id"] for s in STAGES]
    i = ids.index(sid)
    if i + 1 < len(ids) and state[ids[i + 1]]["status"] == "waiting":
        nxt = ids[i + 1]
        state[nxt]["status"] = "ready"
        if nxt == "triage":
            state[nxt]["summary"] = triage_view()
        if nxt == "gate":
            state[nxt]["summary"] = gate_view()


# --- triage and gate ---------------------------------------------------------------------------------------------------
DECISIONS = REPORTS / "decisions.json"


def load_decisions() -> dict:
    return json.loads(DECISIONS.read_text()) if DECISIONS.exists() else {}


def triage_view() -> dict:
    p = REPORTS / "findings.json"
    fs = json.loads(p.read_text()).get("findings", []) if p.exists() else []
    keep = ("id", "title", "severity", "category", "reproducible", "disposition", "location", "evidence",
            "expected_secure_behaviour", "recommended_fix", "reproduction")
    return {"findings": [{k: f.get(k) for k in keep} for f in fs], "decisions": load_decisions()}


def gate_view() -> dict:
    tv, dec = triage_view(), load_decisions()
    runtime = state["runtime"]["summary"] or {}
    golden_broken = [r["case"] for r in runtime.get("rows", []) if r["suite"] == "golden" and r["status"] != "pass"]
    reasons_block, reasons_warn, open_items = [], [], []
    for f in tv["findings"]:
        d = dec.get(f["id"], {})
        choice = d.get("decision")
        if not choice:
            open_items.append(f["id"])
            continue
        if choice == "false_positive":
            continue
        if f["severity"] in ("Critical", "High") and f["reproducible"] and choice == "confirm":
            reasons_block.append(f"{f['id']} {f['severity']} reproducible, confirmed")
        elif choice == "accept_risk":
            if f["severity"] in ("Critical", "High") and f["reproducible"]:
                reasons_block.append(f"{f['id']} is {f['severity']} and reproducible: risk acceptance is a CI-policy waiver, not a gate decision")
            elif not (d.get("owner") and d.get("expires")):
                reasons_block.append(f"{f['id']} accepted without owner and expiry")
            else:
                reasons_warn.append(f"{f['id']} accepted: owner {d['owner']}, expires {d['expires']}")
        elif f["disposition"] == "BLOCK":
            reasons_block.append(f"{f['id']} confirmed with disposition BLOCK")
        elif f["severity"] == "Medium":
            reasons_warn.append(f"{f['id']} Medium, confirmed: needs an owner and expiry")
    if golden_broken:
        reasons_block.append(f"golden cases not passing: {', '.join(golden_broken)}")
    rec = "BLOCK" if reasons_block else ("WARN" if reasons_warn else "PASS")
    return {"recommendation": rec if not open_items else "INCOMPLETE", "block": reasons_block, "warn": reasons_warn,
            "undecided": open_items, "signoff": json.loads((REPORTS / "signoff.json").read_text())
            if (REPORTS / "signoff.json").exists() else None,
            "note": "This is a recommendation. CI policy, not the model or this console, makes the final decision."}


# --- HTTP ----------------------------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):   # DNS-rebinding defence
            self._send(403, {"error": "bad host"})
            return False
        if self.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Console-Token", ""), TOKEN):
            self._send(401, {"error": "missing or wrong token: open the URL printed by server.py"})
            return False
        return True

    def _body(self) -> dict:
        n = min(int(self.headers.get("Content-Length") or 0), 200_000)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (CONSOLE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            with lock:
                stages = [{**s, "commands": [" ".join(c) for c in stage_commands(s["id"])], **state[s["id"]]} for s in STAGES]
            for s in stages:
                if s["id"] == "triage" and s["status"] != "waiting":
                    s["summary"] = triage_view()
                if s["id"] == "gate" and s["status"] != "waiting":
                    s["summary"] = gate_view()
            return self._send(200, {"stages": stages, "review_prompt": REVIEW_PROMPT, "busy": proc is not None})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        parts = urlparse(self.path).path.strip("/").split("/")   # api/stages/<id>/<action>
        body = self._body()
        if parts[:2] == ["api", "decisions"]:
            dec = load_decisions()
            fid = str(body.get("id", ""))[:40]
            dec[fid] = {"decision": body.get("decision") if body.get("decision") in ("confirm", "false_positive", "accept_risk") else None,
                        "owner": str(body.get("owner") or "")[:80], "expires": str(body.get("expires") or "")[:10],
                        "note": str(body.get("note") or "")[:500], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            DECISIONS.write_text(json.dumps(dec, indent=2))
            return self._send(200, {"ok": True})
        if len(parts) != 4 or parts[:2] != ["api", "stages"] or parts[2] not in state:
            return self._send(404, {"error": "not found"})
        sid, action = parts[2], parts[3]
        st = state[sid]
        if action == "run":
            if st["status"] not in ("ready", "review", "failed") or proc is not None:
                return self._send(409, {"error": f"stage is {st['status']}" if proc is None else "another stage is running"})
            if sid in ("triage", "gate"):
                return self._send(400, {"error": "this stage is done by you, not run"})
            st.update(status="running", log=[], summary=None, started=time.time(), ended=None, exit=None)
            threading.Thread(target=run_stage, args=(sid, (body.get("prompt") or "").strip() or None), daemon=True).start()
            return self._send(200, {"ok": True})
        if action in ("accept", "skip"):
            if st["status"] not in ("ready", "review", "failed"):
                return self._send(409, {"error": f"stage is {st['status']}"})
            if sid == "gate" and action == "accept":
                g = gate_view()
                if g["recommendation"] == "INCOMPLETE":
                    return self._send(409, {"error": f"decide every finding first: {', '.join(g['undecided'])}"})
                (REPORTS / "signoff.json").write_text(json.dumps({
                    "recommendation": g["recommendation"], "signed_by": str(body.get("name") or "")[:80],
                    "comment": str(body.get("comment") or "")[:500], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "block": g["block"], "warn": g["warn"], "note": g["note"]}, indent=2))
            st["status"] = "accepted" if action == "accept" else "skipped"
            unlock_next(sid)
            return self._send(200, {"ok": True})
        if action == "stop" and st["status"] == "running" and proc is not None:
            proc.terminate()
            log(sid, "error", "stopped by you")
            return self._send(200, {"ok": True})
        self._send(400, {"error": "unknown action"})


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Review Console: http://127.0.0.1:{PORT}/#token={TOKEN}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
