"""Per-session conversation transcripts: one .txt file per chat session in var/conversations/.

Each turn records the fan's question (PII-redacted), the route, every agent and tool call with arguments,
timings and results, and the final answer. Files are named <started YYYYmmdd-HHMMSS>_<session hash>.txt;
the session id itself never appears on disk or in a URL.
"""

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from .logging import redact

NAME = re.compile(r"^\d{8}-\d{6}_[0-9a-f]{12}\.txt$")
MAX_FILE_BYTES = 2_000_000  # a runaway session stops growing here
KEEP_FILES = 500            # oldest transcripts are deleted beyond this
RESULT_CHARS = 700          # tool results are cut to this many characters per call


def session_key(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()[:12]


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + f" … [+{len(text) - n} chars]"


class TranscriptStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, Path] = {}
        self._turns: dict[str, int] = {}
        self._lock = threading.Lock()

    # --- reading -----------------------------------------------------------------
    def path_for(self, sid: str, create: bool = False) -> Path | None:
        key = session_key(sid)
        with self._lock:
            p = self._files.get(key)
            if p is None:
                existing = sorted(self.folder.glob(f"*_{key}.txt"))
                if existing:
                    p = existing[-1]
                elif create:
                    p = self.folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{key}.txt"
                    p.write_text(f"Crickbot conversation transcript\nSession: {key} (hashed)\n"
                                 f"Started: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
                    self._prune()
                if p:
                    self._files[key] = p
            return p

    def files(self) -> list[dict]:
        out = []
        for p in sorted(self.folder.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True):
            if NAME.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "bytes": st.st_size, "modified": st.st_mtime,
                            "turns": p.read_text(errors="replace").count("\n==== Turn ")})
        return out

    def read(self, name: str) -> str | None:
        """Read one transcript by file name. Names are validated, so nothing outside the folder can be read."""
        if not NAME.match(name):
            return None
        p = (self.folder / name).resolve()
        if p.parent != self.folder.resolve() or not p.is_file():
            return None
        return p.read_text(errors="replace")

    # --- writing -----------------------------------------------------------------
    def record_turn(self, sid: str, question: str, events: list[dict]) -> None:
        p = self.path_for(sid, create=True)
        with self._lock:
            if p.stat().st_size > MAX_FILE_BYTES:
                return
            n = self._turns.get(p.name)
            if n is None:
                n = p.read_text(errors="replace").count("\n==== Turn ")
            n += 1
            self._turns[p.name] = n
            text = format_turn(n, question, events)
            if p.stat().st_size + len(text) > MAX_FILE_BYTES:
                text += "\n[transcript size limit reached: later turns are not recorded]\n"
            with p.open("a") as f:
                f.write(text)

    def _prune(self) -> None:
        files = sorted(self.folder.glob("*.txt"), key=lambda p: p.stat().st_mtime)
        for old in files[:max(0, len(files) - KEEP_FILES)]:
            old.unlink(missing_ok=True)


def format_turn(n: int, question: str, events: list[dict]) -> str:
    """One readable block per turn, built from the same events the browser receives (plus internal evidence)."""
    done = next((e for e in events if e.get("type") == "done"), {})
    lines = [f"\n==== Turn {n} · {time.strftime('%Y-%m-%d %H:%M:%S')} ====",
             f"FAN: {redact(question)}"]
    for e in events:
        t = e.get("type")
        if t == "ids":
            lines.append(f"IDS: correlation {e.get('correlation_id')} · trace {e.get('trace_id')} · turn run {e.get('run_id')}")
        elif t == "route":
            lines.append(f"ROUTER ({e.get('method', '?')}): {', '.join(e.get('routes', [])) or 'none'}"
                         + (f" · {e['reason']}" if e.get("reason") else ""))
        elif t == "agent":
            ms = f" · {e['ms']} ms" if e.get("ms") is not None else ""
            lines.append(f"  [agent] {e.get('name')} · {e.get('status', '')}{ms}"
                         + (f"\n    output: {_clip(e['output'], 900)}" if e.get("output") else ""))
        elif t == "tool":
            ms = f"{e['ms']} ms" if e.get("ms") is not None else "-"
            who = f"{e['agent']} → " if e.get("agent") else ""
            lines.append(f"  [tool] {who}{e.get('name')}({json.dumps(e.get('args') or {}, ensure_ascii=False)}) "
                         f"{'ok' if e.get('ok', True) else 'FAILED'} · {ms}"
                         + (f" · hits {e['hits']}" if e.get("hits") else "") + (f" · {e['error']}" if e.get("error") else ""))
        elif t == "thinking":
            continue  # reasoning tokens are long; their length is recorded on the done event
        elif t == "error":
            lines.append(f"  [error] {e.get('message')} ({e.get('detail', '')})")
    for ev in done.get("evidence") or []:
        lines.append(f"  [result] {ev.get('source')}: {_clip(ev.get('text', ''), RESULT_CHARS)}")
    if done:
        lines.append(f"PATH: {done.get('path')} · model {done.get('model') or '-'} · degrade level {done.get('degrade_level')}")
        lines.append(f"ANSWER ({done.get('latency_ms')} ms, first word {done.get('first_token_ms')} ms, "
                     f"{done.get('tokens') or 0} tokens): {done.get('answer')}")
        if done.get("removed_sentences"):
            lines.append(f"VALIDATOR: removed {done['removed_sentences']} sentence(s) with unsupported numbers")
        if done.get("unverified"):
            lines.append(f"VALIDATOR: unverified numbers {done['unverified']}")
        src = ", ".join(f"{s.get('type')}:{s.get('id')}" for s in done.get("sources") or [])
        lines.append(f"SOURCES: {src or 'none'}")
        if done.get("reason"):
            lines.append(f"REASON: {done['reason']}")
    return "\n".join(lines) + "\n"
