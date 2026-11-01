"""Searchable tail of the app's JSON log file (backend/var/app.log) for the observability dashboard.

Reads only the end of the file (the newest lines), so it stays fast however large the log grows. Every line was
already redacted when it was written.
"""

import json
from pathlib import Path

TAIL_BYTES = 3_000_000
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


POLLING = ("/api/admin/", "/api/ops/", "/api/live", "/metrics", "/health/")


def tail_logs(path: Path, *, q: str = "", level: str = "", logger: str = "", agent: str = "", correlation_id: str = "",
              run_id: str = "", limit: int = 200, hide_polling: bool = False) -> dict:
    """hide_polling drops the request lines of dashboards and tickers polling the API, which otherwise bury everything."""
    if not path.exists():
        return {"lines": [], "scanned": 0, "file": path.name}
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        raw = f.read().decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES:
        raw = raw[1:]  # the first line may be cut in half
    min_level = LEVELS.get(level.upper(), 0)
    q = q.lower()
    out = []
    for line in reversed(raw):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if LEVELS.get(rec.get("level", ""), 0) < min_level:
            continue
        if hide_polling and rec.get("logger") == "http" and str(rec.get("path", "")).startswith(POLLING):
            continue
        if logger and rec.get("logger") != logger:
            continue
        if agent and rec.get("agent") != agent:
            continue
        if correlation_id and correlation_id not in (rec.get("correlation_id"), rec.get("request_id")):
            continue
        if run_id and run_id not in (rec.get("run_id"), rec.get("parent_run_id")):
            continue
        if q and q not in line.lower():
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return {"lines": out, "scanned": len(raw), "file": path.name, "bytes": size}
