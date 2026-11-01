#!/usr/bin/env python3
"""PreToolUse guard for the security-reviewer sub-agent.

It reads the hook JSON on stdin. Exit 0 hands the call to the normal permission flow (you are still asked);
exit 2 blocks the call, and stderr tells the agent why. Every decision is appended to reports/guard.log.

Enforced:
  Read/Grep/Glob  never on credential files (.env, ~/.ssh, ~/.aws, keys...). A Grep over a folder needs a glob/type.
  Write/Edit      only under security-reviewer/corpus/ and security-reviewer/reports/.
  Bash            one allowlisted command, with no shell operators:
                  git (read-only subcommands), semgrep, trivy, gitleaks, promptfoo, and curl to http://localhost:8080.
  anything else   blocked (the agent's tool list should not offer it anyway).
"""
import json
import os
import re
import shlex
import sys
import time

SR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # .../security-reviewer
WRITABLE = (os.path.join(SR, "corpus") + os.sep, os.path.join(SR, "reports") + os.sep)
REPORTS = WRITABLE[1]
LOCAL = ("http://localhost:8080", "http://127.0.0.1:8080")

SECRET_PARTS = {".ssh", ".aws", ".gnupg", ".kube", ".azure", ".config/gcloud", ".docker", ".netrc", ".git-credentials",
                ".pgpass", ".npmrc", ".pypirc"}
SECRET_NAME = re.compile(r"(^\.env(\..+)?$|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|p12|pfx|jks|kdbx)$|^credentials(\..+)?$"
                         r"|^secrets?\.(json|ya?ml|toml)$|service[-_]account.*\.json$)", re.I)
SAFE_ENV = re.compile(r"^\.env\.(example|sample|template|dist)$", re.I)
SHELL_OPS = re.compile(r"[;&|`$<>(){}\n\\]")
ENV_OK = {"PROMPTFOO_DISABLE_TELEMETRY", "PROMPTFOO_DISABLE_UPDATE", "SEMGREP_SEND_METRICS", "NO_COLOR"}


def log(decision: str, tool: str, detail: str) -> None:
    try:
        os.makedirs(REPORTS, exist_ok=True)
        detail = re.sub(r"(?i)(bearer\s+|token[=:]\s*)\S+", r"\1[REDACTED]", detail)[:400]
        with open(os.path.join(REPORTS, "guard.log"), "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "decision": decision, "tool": tool,
                                "detail": detail}) + "\n")
    except OSError:
        pass


def block(tool: str, why: str, detail: str = "") -> None:
    log("deny", tool, f"{why} | {detail}")
    print(f"security-reviewer guard: blocked. {why}", file=sys.stderr)
    sys.exit(2)


def is_secret(path: str) -> bool:
    p = os.path.realpath(os.path.expanduser(path))
    if any(f"/{s}/" in p + "/" for s in SECRET_PARTS):
        return True
    name = os.path.basename(p)
    return bool(SECRET_NAME.search(name)) and not SAFE_ENV.match(name)


def under(path: str, roots, cwd: str) -> bool:
    p = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
    return any((p + os.sep).startswith(r) for r in roots)


def check_outputs(args, flags, cwd, tool):
    """Every value of an output flag must land in security-reviewer/reports/."""
    for i, a in enumerate(args):
        for fl in flags:
            val = None
            if a == fl and i + 1 < len(args):
                val = args[i + 1]
            elif a.startswith(fl + "="):
                val = a.split("=", 1)[1]
            if val is not None and not under(val, (REPORTS,), cwd):
                block(tool, f"{fl} must write under security-reviewer/reports/", val)


def check_bash(cmd: str, cwd: str) -> None:
    if SHELL_OPS.search(cmd):
        block("Bash", "shell operators (; & | ` $ < > ( ) \\ newline) are not allowed; run one command", cmd)
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        block("Bash", f"unparseable command: {e}", cmd)
    while argv and re.match(r"^[A-Z_]+=", argv[0]):
        if argv[0].split("=")[0] not in ENV_OK:
            block("Bash", f"environment variable not allowed: {argv[0].split('=')[0]}", cmd)
        argv = argv[1:]
    if not argv:
        block("Bash", "empty command", cmd)
    prog, args = os.path.basename(argv[0]), argv[1:]
    for a in args:
        if not a.startswith("-") and "/" in a and is_secret(a):
            block("Bash", "argument points at a credential file", a)

    if args in (["--version"], ["version"]):
        return
    if prog == "git":
        if not args or args[0] not in {"diff", "log", "blame", "show", "status", "rev-parse", "ls-files"}:
            block("Bash", "git: only diff/log/blame/show/status/rev-parse/ls-files", cmd)
        if any(a.startswith(("--output", "--ext-diff", "--exec", "--upload-pack", "--no-index", "--textconv")) for a in args):
            block("Bash", "git: output/exec/--no-index options are not allowed (--no-index diffs any file, .env included)", cmd)
    elif prog == "semgrep":
        if args and args[0] not in {"scan", "ci"} and not args[0].startswith("-"):
            block("Bash", "semgrep: only `semgrep scan`", cmd)
        if args and args[0] == "ci":
            block("Bash", "semgrep ci talks to Semgrep's cloud", cmd)
        if "--metrics=off" not in args and not ("--metrics" in args and args[args.index("--metrics") + 1:][:1] == ["off"]):
            block("Bash", "semgrep: add --metrics=off", cmd)
        if any(a.startswith("--autofix") or a == "--dryrun" for a in args):
            block("Bash", "semgrep: autofix edits code", cmd)
        cfgs = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--config", "-c", "-f")] + \
               [a.split("=", 1)[1] for a in args if a.startswith("--config=")]
        if not cfgs or any(c == "auto" or re.match(r"^(p|r|s)/|^https?:", c) for c in cfgs):
            block("Bash", "semgrep: --config must be a local rules path (registry configs need network; ask the user)", cmd)
        check_outputs(args, ["--output", "-o", "--sarif-output", "--json-output", "--text-output"], cwd, "Bash")
    elif prog == "trivy":
        if not args or args[0] not in {"fs", "filesystem", "config"}:
            block("Bash", "trivy: only `trivy fs` or `trivy config`", cmd)
        if args[0] in {"fs", "filesystem"} and "--skip-db-update" not in args:
            block("Bash", "trivy fs: add --skip-db-update (DB downloads need the user's approval)", cmd)
        for flag in ("--skip-check-update", "--skip-version-check"):  # otherwise trivy fetches its checks bundle / version notice
            if flag not in args:
                block("Bash", f"trivy: add {flag} (no network)", cmd)
        check_outputs(args, ["--output", "-o"], cwd, "Bash")
    elif prog == "gitleaks":
        if not args or args[0] not in {"dir", "git", "detect"}:
            block("Bash", "gitleaks: only `gitleaks dir` or `gitleaks git`", cmd)
        if "--redact" not in args and not any(a.startswith("--redact=") for a in args):
            block("Bash", "gitleaks: add --redact so secrets never reach the transcript", cmd)
        check_outputs(args, ["--report-path", "-r"], cwd, "Bash")
    elif prog == "promptfoo":
        if not args or args[0] != "eval":
            block("Bash", "promptfoo: only `promptfoo eval`", cmd)
        for var in ("PROMPTFOO_DISABLE_TELEMETRY", "PROMPTFOO_DISABLE_UPDATE"):  # no telemetry, no update check
            if os.environ.get(var) != "1" and f"{var}=1" not in cmd:
                block("Bash", f"promptfoo: prefix with {var}=1", cmd)
        if "--share" in args:
            block("Bash", "promptfoo: --share uploads results", cmd)
        cfgs = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-c", "--config")]
        if not cfgs or not all(under(c, (WRITABLE[0],), cwd) for c in cfgs):
            block("Bash", "promptfoo: -c must point inside security-reviewer/corpus/", cmd)
        check_outputs(args, ["--output", "-o"], cwd, "Bash")
    elif prog == "curl":
        urls = [a for a in args if re.match(r"^[a-z]+://", a, re.I)]
        if not urls or any(not any(u == b or u.startswith(b + "/") for b in LOCAL) for u in urls):
            block("Bash", "curl: only http://localhost:8080 (or 127.0.0.1:8080)", cmd)
        bad = {"-o", "--output", "-O", "--remote-name", "-T", "--upload-file", "-K", "--config", "-L", "--location",
               "-x", "--proxy", "--resolve", "--connect-to", "-F", "--form"}
        if any(a in bad or a.split("=")[0] in bad for a in args):
            block("Bash", "curl: output/upload/redirect/proxy options are not allowed", cmd)
        if any("@" in a for a in args):  # "@file", "-d@file", "--data-binary=@file" all read a local file
            block("Bash", "curl: '@' is not allowed (@file bodies read local files)", cmd)
    else:
        block("Bash", f"`{prog}` is not on the allowlist (git, semgrep, trivy, gitleaks, promptfoo, curl→localhost:8080)", cmd)


def main() -> None:
    data = json.load(sys.stdin)
    # Registered project-wide (.claude/settings.json) so it runs regardless of how the agent file was loaded; it only
    # judges calls made inside the security-reviewer sub-agent and stays out of every other agent's way.
    if data.get("agent_type") != "security-reviewer":
        sys.exit(0)
    tool, ti = data.get("tool_name", ""), data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    if tool in ("Write", "Edit", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or ""
        if not under(fp, WRITABLE, cwd):
            block(tool, "writes are allowed only under security-reviewer/corpus/ and security-reviewer/reports/", fp)
    elif tool == "Read":
        if is_secret(ti.get("file_path", "")):
            block(tool, "credential files are off limits", ti.get("file_path", ""))
    elif tool in ("Grep", "Glob"):
        path = ti.get("path") or cwd
        if is_secret(path):
            block(tool, "credential files are off limits", path)
        pat = (ti.get("glob") or "") + " " + (ti.get("pattern") if tool == "Glob" else "")
        if re.search(r"\.env\b(?!\.example)|\.ssh|\.aws|id_rsa|\.pem\b", pat):
            block(tool, "pattern targets credential files", pat)
        if tool == "Grep" and os.path.isdir(os.path.join(cwd, path)) and not (ti.get("glob") or ti.get("type")):
            block(tool, "Grep over a folder needs `glob` or `type` (keeps .env and keys out of results)", path)
    elif tool == "Bash":
        check_bash(ti.get("command", ""), cwd)
    elif tool == "SubagentHandback":
        pass  # the harness tool that returns the final report to the main session; no side effects
    elif tool.startswith(("mcp__semgrep__", "mcp__trivy__")):
        pass  # inputs are server-specific; logged below and still subject to the normal permission prompt
    else:
        block(tool, "tool not allowed for security-reviewer", "")
    log("allow", tool, json.dumps(ti)[:400])
    sys.exit(0)


if __name__ == "__main__":
    main()
