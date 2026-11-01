"""End-to-end RAG + LLM quality evaluation.

Runs the real agent in-process (real data, real Ollama) over knowledge/answer_quality_eval.json and reports:
  1. Retrieval: recall@1/3/5 and MRR against labelled chunks, and context precision of the chunks the agent used.
  2. Answers: required facts present, known-wrong strings absent.
  3. Groundedness: each claim (sentence) checked against the evidence the agent actually had:
     numbers by exact match, everything else by an LLM judge.
  4. Triage, retrieval first: a bad answer is a "retrieval miss" if the needed source never reached the agent,
     otherwise a "generation error".
  5. Latency per stage (embed, search, tools, LLM, other) from the OpenTelemetry spans, p50/p95.
The judge is calibrated first on knowledge/judge_calibration.json; if it disagrees with the human labels too often,
its verdicts are reported as advisory and never fail an answer.

Usage: uv run python -m cricket_chatbot.rag.quality [--no-judge] [--limit N] [--calibrate-only]
"""

import argparse
import asyncio
import json
import re
import statistics
import time
from pathlib import Path

import httpx

from ..config import BACKEND_DIR, get_settings

TRUST_ACCURACY = 0.85  # judge must agree with human labels this often...
TRUST_KAPPA = 0.70     # ...and well beyond chance (Cohen's kappa), or its verdicts are advisory only
NUM = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------- metrics (pure, unit-tested)
def recall_at_k(ranked: list[str], relevant: list[str], k: int) -> float | None:
    """Share of the relevant chunks found in the top k. None when the question has no labelled chunks."""
    if not relevant:
        return None
    return len(set(ranked[:k]) & set(relevant)) / len(set(relevant))


def reciprocal_rank(ranked: list[str], relevant: list[str]) -> float | None:
    if not relevant:
        return None
    return next((1 / (i + 1) for i, d in enumerate(ranked) if d in relevant), 0.0)


def context_precision(used: list[str], relevant: list[str]) -> float | None:
    """Share of the chunks the agent used that are labelled relevant (a lower bound: labels can be incomplete)."""
    if not used or not relevant:
        return None
    return len([u for u in used if u in relevant]) / len(used)


def split_claims(answer: str) -> list[str]:
    """One claim per sentence; very short fragments ("Sure!") carry no checkable fact."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", answer.strip())
    return [p.strip() for p in parts if len(p.split()) >= 4]


def numbers_of(text: str) -> set[str]:
    out = set()
    for n in NUM.findall(text.replace(",", "")):
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def unsupported_numbers(claim: str, evidence: str, question: str = "") -> list[str]:
    """Numbers in the claim that appear nowhere in the evidence or the question. 0-20 are allowed unless they're a stat."""
    from ..agent.agent import _stat_counts  # same rule as the app: a small number that is a stat must be in the data

    known = numbers_of(evidence) | numbers_of(question)
    stats = {str(n) for n in _stat_counts(claim)}
    return sorted(n for n in numbers_of(claim) if n not in known and (n in stats or not (n.isdigit() and int(n) <= 20)))


def check_answer(answer: str, must: list[list[str]], must_not: list[str]) -> list[str]:
    low = answer.lower()
    problems = [f"missing one of {alts}" for alts in must if not any(a.lower() in low for a in alts)]
    problems += [f"contains '{bad}'" for bad in must_not if bad.lower() in low]
    return problems


def retrieval_ok(evidence_sources: list[str], needs_any: list[str]) -> bool:
    return not needs_any or any(s in needs_any for s in evidence_sources)


def triage(retrieved: bool, answer_problems: list[str], ungrounded: int) -> str:
    """Check retrieval first: a wrong answer built on missing evidence is a retrieval problem, not a model problem."""
    if not answer_problems and not ungrounded:
        return "ok"
    return "generation_error" if retrieved else "retrieval_miss"


def calibration_stats(pairs: list[tuple[str, str]]) -> dict:
    """pairs of (human_label, judge_label). Positive class = 'unsupported' (what the judge exists to catch)."""
    n = len(pairs)
    labels = ("supported", "unsupported")
    cm = {(h, j): sum(1 for a, b in pairs if a == h and b == j) for h in labels for j in labels}
    agree = cm[("supported", "supported")] + cm[("unsupported", "unsupported")]
    po = agree / n if n else 0.0
    pe = sum((sum(cm[(h, j)] for j in labels) / n) * (sum(cm[(x, h)] for x in labels) / n) for h in labels) if n else 0.0
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    tp, fn, fp = cm[("unsupported", "unsupported")], cm[("unsupported", "supported")], cm[("supported", "unsupported")]
    return {"items": n, "accuracy": round(po, 3), "kappa": round(kappa, 3),
            "catches_unsupported": round(tp / (tp + fn), 3) if tp + fn else None,   # recall on bad claims
            "false_alarms": round(fp / (fp + cm[("supported", "supported")]), 3) if fp + cm[("supported", "supported")] else None,
            "confusion": {f"human_{h}/judge_{j}": v for (h, j), v in cm.items()},
            "trusted": po >= TRUST_ACCURACY and kappa >= TRUST_KAPPA}


def stage_latency(trace: dict) -> dict:
    """Split one chat trace into stages. rag.search includes its embed call, so search = rag.search - embed."""
    spans = trace.get("spans", [])
    total = trace.get("duration_ms", 0.0)
    s = lambda pred: sum(x["duration_ms"] for x in spans if pred(x["name"]))
    embed = s(lambda n: n == "ollama.embed")
    search = max(s(lambda n: n == "rag.search") - embed, 0.0)
    tools = s(lambda n: n.startswith("tool.") and n != "tool.search_cricket_knowledge")
    llm = s(lambda n: n == "ollama.chat")
    return {"embed": round(embed, 1), "search": round(search, 1), "tools": round(tools, 1), "llm": round(llm, 1),
            "other": round(max(total - embed - search - tools - llm, 0.0), 1), "total": round(total, 1)}


def grounded_share(claims: list[dict]) -> float | None:
    """Share of gradable claims that are grounded. A claim the judge couldn't grade is left out, not counted as bad."""
    graded = [x for x in claims if x.get("judge") != "error" or x["bad_numbers"]]
    if not graded:
        return None
    return 1 - sum(1 for x in graded if x["bad_numbers"] or x.get("judge") == "unsupported") / len(graded)


def pct(values: list[float], p: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return round(v[min(len(v) - 1, max(0, round(p / 100 * len(v) + 0.5) - 1))], 1)


# ---------------------------------------------------------------------------- the judge
JUDGE_SYSTEM = (
    "You are a strict fact-checker for a cricket chatbot. Decide whether the CLAIM is fully supported by the EVIDENCE.\n"
    "supported: every name, number, comparison and cause in the claim is stated in the evidence or follows directly from it "
    "(for example, comparing two numbers that are both in the evidence).\n"
    "unsupported: any part is missing from the evidence, contradicts it, compares numbers the wrong way, or relies on outside "
    "knowledge, even if it is true in real life.\n"
    "First quote the few evidence words the claim depends on (at most 30 words, or 'none'), then check every part of the "
    "claim against them, then give the verdict. Keep the check under 40 words. Answer with JSON only.")
# Quote and check come before the verdict, so the model reasons before it commits.
JUDGE_FORMAT = {"type": "object", "properties": {"quote": {"type": "string"}, "check": {"type": "string"},
                                                 "verdict": {"type": "string", "enum": ["supported", "unsupported"]}},
                "required": ["quote", "check", "verdict"]}


class Judge:
    def __init__(self, url: str, model: str):
        self.model = model
        self.http = httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(180, connect=5))

    async def verdict(self, evidence: str, claim: str) -> tuple[str, str]:
        """'supported' / 'unsupported', or 'error' when the judge gives no usable answer twice (never counted as a verdict)."""
        for _ in range(2):
            v, why = await self._ask(evidence, claim)
            if v != "error":
                return v, why
        return "error", why

    async def _ask(self, evidence: str, claim: str) -> tuple[str, str]:
        body = {"model": self.model, "stream": False, "format": JUDGE_FORMAT, "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 600},
                "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                             {"role": "user", "content": f"EVIDENCE:\n{evidence[:6000]}\n\nCLAIM: {claim}"}]}
        r = await self.http.post("/api/chat", json=body)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        try:
            out = json.loads(content)
            v = out.get("verdict", "")
            return (v, str(out.get("check", ""))[:200]) if v in ("supported", "unsupported") else ("error", "no verdict")
        except (json.JSONDecodeError, TypeError, AttributeError):
            m = re.search(r'"verdict"\s*:\s*"(supported|unsupported)"', content)
            return (m.group(1), "partial output") if m else ("error", "judge output cut off or unparseable")

    async def close(self) -> None:
        await self.http.aclose()


async def calibrate(judge: Judge) -> dict:
    data = json.loads((BACKEND_DIR / "knowledge" / "judge_calibration.json").read_text())
    # Calibrate the grader exactly as the evaluation uses it: numbers by exact match first, then the LLM judge.
    pairs, llm_pairs, wrong = [], [], []
    for it in data["items"]:
        llm_got, why = await judge.verdict(it["evidence"], it["claim"])
        bad = unsupported_numbers(it["claim"], it["evidence"])
        if llm_got == "error":  # no answer counts against the judge
            llm_got = "supported" if it["label"] == "unsupported" else "unsupported"
        got = "unsupported" if bad else llm_got
        pairs.append((it["label"], got))
        llm_pairs.append((it["label"], llm_got))
        if got != it["label"]:
            wrong.append({"id": it["id"], "claim": it["claim"], "human": it["label"], "judge": got, "judge_reason": why})
    stats = calibration_stats(pairs)
    stats["llm_alone"] = {k: v for k, v in calibration_stats(llm_pairs).items() if k not in ("confusion", "trusted")}
    # The judge prompt was revised once after looking at disagreements on odd-numbered items only; even-numbered
    # items were never inspected, so their score is the honest one.
    ids = [it["id"] for it in data["items"]]
    stats["untouched_half"] = {k: v for k, v in calibration_stats([p for i, p in zip(ids, pairs) if i % 2 == 0]).items() if k != "confusion"}
    stats["trusted"] = stats["trusted"] and stats["untouched_half"]["trusted"]
    stats["labels_reviewed_by_human"] = bool(data.get("reviewed"))
    stats["disagreements"] = wrong
    return stats


# ---------------------------------------------------------------------------- the run
async def _build_agent(settings):
    from ..agent.agent import Agent, AgentDeps
    from ..agent.memory import SessionMemory, SingleFlight, TTLCache
    from ..ingest import ensure_database
    from ..live.feed import LiveFeed
    from ..llm.ollama import OllamaClient
    from ..observability.tracing import setup_tracing
    from ..resilience.health import HealthRegistry
    from ..resilience.load_shed import AdmissionController
    from .store import KnowledgeStore

    setup_tracing("cricket-chatbot-eval", None)
    health = HealthRegistry()
    health.admission = AdmissionController(1, 6, settings.llm_queue_timeout_s)
    health.breaker("stats_db", failure_threshold=settings.breaker_failure_threshold, reset_after_s=settings.breaker_reset_s)
    stats = ensure_database(settings=settings)
    llm = OllamaClient(settings.ollama_url, health, timeout_s=settings.llm_timeout_s, connect_timeout_s=settings.llm_connect_timeout_s,
                       embed_timeout_s=settings.embed_timeout_s, breaker_threshold=settings.breaker_failure_threshold,
                       breaker_reset_s=settings.breaker_reset_s)
    knowledge = KnowledgeStore(settings.chroma_path, settings.chroma_collection, llm, settings.embed_model, health,
                               settings.breaker_failure_threshold, settings.breaker_reset_s)
    feed = LiveFeed(stats, settings.replay_match_id, settings.replay_ball_interval_s, health)
    for _ in range(150):  # a fixed point in the second innings, so live answers are repeatable
        feed.apply_next()
    cache = TTLCache(1, 0.001)  # effectively off: every question is answered fresh
    agent = Agent(AgentDeps(settings=settings, llm=llm, stats=stats, knowledge=knowledge, feed=feed, health=health,
                            admission=health.admission, cache=cache, flights=SingleFlight(), sessions=SessionMemory()))
    return agent, knowledge, llm


async def run(use_judge: bool, limit: int | None, calibrate_only: bool) -> dict:
    from ..observability.tracing import recent_traces

    settings = get_settings()
    judge = Judge(settings.ollama_url, settings.chat_model) if use_judge else None
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "judge_model": settings.chat_model if judge else None}
    try:
        if judge:
            print("Calibrating the judge on human-labelled claims ...", flush=True)
            report["calibration"] = await calibrate(judge)
            c = report["calibration"]
            u = c["untouched_half"]
            la = c["llm_alone"]
            print(f"  LLM judge alone: accuracy {la['accuracy']}  kappa {la['kappa']}  catches unsupported {la['catches_unsupported']}")
            print(f"  grader (exact numbers + LLM judge), all {c['items']}: accuracy {c['accuracy']}  kappa {c['kappa']}  catches unsupported {c['catches_unsupported']}  "
                  f"false alarms {c['false_alarms']}\n  untouched half ({u['items']}): accuracy {u['accuracy']}  kappa {u['kappa']}"
                  f"\n  -> {'TRUSTED' if c['trusted'] else 'NOT TRUSTED (advisory only)'}", flush=True)
            if not c["labels_reviewed_by_human"]:
                print("  WARNING: the calibration labels have not been reviewed by a human yet "
                      "(knowledge/judge_calibration.json, set \"reviewed\": true after checking them).", flush=True)
            if calibrate_only:
                return report
        trusted = bool(judge and report["calibration"]["trusted"])

        agent, knowledge, llm = await _build_agent(settings)
        cases = json.loads((BACKEND_DIR / "knowledge" / "answer_quality_eval.json").read_text())["cases"][:limit]
        rows = []
        for i, c in enumerate(cases, 1):
            # 1. Retrieval on its own, against the labels.
            ranked = [h["id"] for h in await knowledge.search(c["q"], k=5, max_distance=2.0)] if c["relevant_docs"] else []
            # 2. The real answer.
            done = None
            async for ev in agent.answer(c["q"], f"eval-{i}"):
                if ev["type"] == "done":
                    done = ev
            trace = next((t for t in recent_traces.recent(5) if t["root"] == "chat.answer"), {})
            evidence = done.get("evidence", [])
            ev_sources = [e["source"] for e in evidence]
            ev_text = "\n\n".join(f"[{e['source']}] {e['text']}" for e in evidence)
            used_docs = [s.removeprefix("doc:") for s in ev_sources if s.startswith("doc:")]
            problems = check_answer(done["answer"], c.get("must", []), c.get("must_not", []))
            retrieved = retrieval_ok(ev_sources, c["needs_any"])
            # 3. Groundedness per claim. Refusals and scope replies have nothing to ground.
            claims = []
            if evidence and done["path"] not in ("blocked", "scope", "off_topic", "clarify"):
                for cl in split_claims(done["answer"]):
                    row = {"claim": cl, "bad_numbers": unsupported_numbers(cl, ev_text, c["q"])}
                    if judge:
                        row["judge"], row["judge_reason"] = await judge.verdict(ev_text, cl)
                    claims.append(row)
            hard_fail = sum(1 for x in claims if x["bad_numbers"])
            judge_fail = sum(1 for x in claims if x.get("judge") == "unsupported")
            ungrounded = hard_fail + (judge_fail if trusted else 0)
            r = {"q": c["q"], "kind": c["kind"], "path": done["path"], "answer": done["answer"],
                 "recall@1": recall_at_k(ranked, c["relevant_docs"], 1), "recall@3": recall_at_k(ranked, c["relevant_docs"], 3),
                 "recall@5": recall_at_k(ranked, c["relevant_docs"], 5), "rr": reciprocal_rank(ranked, c["relevant_docs"]),
                 "top5": ranked, "used_sources": ev_sources, "context_precision": context_precision(used_docs, c["relevant_docs"]),
                 "retrieval_ok": retrieved, "answer_problems": problems, "claims": claims,
                 "grounded": grounded_share(claims),
                 "verdict": triage(retrieved, problems, ungrounded), "latency": stage_latency(trace),
                 "first_token_ms": done.get("first_token_ms")}
            rows.append(r)
            flag = {"ok": "ok ", "retrieval_miss": "RETRIEVAL MISS", "generation_error": "GENERATION ERROR"}[r["verdict"]]
            print(f"[{i:2}/{len(cases)}] {flag:16} {r['path']:9} {r['latency']['total']:8.0f} ms  {c['q']}", flush=True)
        await llm.close()
        report["cases"] = rows
        report["summary"] = summarise(rows, trusted, bool(judge))
        return report
    finally:
        if judge:
            await judge.close()


def summarise(rows: list[dict], trusted: bool, judged: bool) -> dict:
    mean = lambda xs: round(statistics.mean(xs), 3) if xs else None
    col = lambda k: [r[k] for r in rows if r[k] is not None]
    stages = ("embed", "search", "tools", "llm", "other", "total")
    by_path: dict[str, list[float]] = {}
    for r in rows:
        by_path.setdefault(r["path"], []).append(r["latency"]["total"])
    all_claims = [x for r in rows for x in r["claims"]]
    return {
        "questions": len(rows),
        "retrieval": {"recall@1": mean(col("recall@1")), "recall@3": mean(col("recall@3")), "recall@5": mean(col("recall@5")),
                      "mrr": mean(col("rr")), "context_precision": mean(col("context_precision")),
                      "needed_source_reached_agent": mean([1.0 if r["retrieval_ok"] else 0.0 for r in rows])},
        "answers": {"correct": sum(1 for r in rows if not r["answer_problems"]), "of": len(rows)},
        "groundedness": {"claims": len(all_claims), "claims_with_unsupported_numbers": sum(1 for x in all_claims if x["bad_numbers"]),
                         "judge_unsupported": sum(1 for x in all_claims if x.get("judge") == "unsupported") if judged else None,
                         "judge_errors": sum(1 for x in all_claims if x.get("judge") == "error") if judged else None,
                         "judge_counts": "yes" if trusted else ("advisory only (judge not trusted)" if judged else "no judge"),
                         "mean_grounded": mean(col("grounded"))},
        "triage": {v: sum(1 for r in rows if r["verdict"] == v) for v in ("ok", "retrieval_miss", "generation_error")},
        "latency_ms": {st: {"p50": pct([r["latency"][st] for r in rows], 50), "p95": pct([r["latency"][st] for r in rows], 95)} for st in stages},
        "latency_by_path_ms": {p: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95)} for p, v in sorted(by_path.items())},
    }


def print_report(rep: dict) -> None:
    s = rep.get("summary")
    if not s:
        return
    r, g, t = s["retrieval"], s["groundedness"], s["triage"]
    print("\n=== Retrieval (labelled chunks) ===")
    print(f"recall@1 {r['recall@1']}  recall@3 {r['recall@3']}  recall@5 {r['recall@5']}  MRR {r['mrr']}  "
          f"context precision {r['context_precision']}  needed source reached the agent {r['needed_source_reached_agent']}")
    print("\n=== Answers ===")
    print(f"correct facts: {s['answers']['correct']}/{s['answers']['of']}")
    print(f"claims: {g['claims']}  with unsupported numbers: {g['claims_with_unsupported_numbers']}  judge says unsupported: "
          f"{g['judge_unsupported']}  judge errors: {g['judge_errors']}  ({g['judge_counts']})  mean groundedness {g['mean_grounded']}")
    print(f"triage: ok {t['ok']}  retrieval miss {t['retrieval_miss']}  generation error {t['generation_error']}")
    print("\n=== Latency per stage (ms) ===")
    print("stage    " + "".join(f"{k:>9}" for k in s["latency_ms"]))
    for p in ("p50", "p95"):
        print(f"{p:8} " + "".join(f"{(v[p] if v[p] is not None else '-'):>9}" for v in s["latency_ms"].values()))
    print("by path: " + ", ".join(f"{p} n={v['n']} p50={v['p50']} p95={v['p95']}" for p, v in s["latency_by_path_ms"].items()))
    bad = [c for c in rep["cases"] if c["verdict"] != "ok"]
    if bad:
        print("\n=== Failures (retrieval checked first) ===")
        for c in bad:
            print(f"- {c['q']}\n    verdict: {c['verdict']}  path: {c['path']}  sources used: {c['used_sources']}")
            for p in c["answer_problems"]:
                print(f"    answer: {p}")
            for x in c["claims"]:
                if x["bad_numbers"] or x.get("judge") == "unsupported":
                    print(f"    claim: {x['claim'][:110]}\n      numbers not in evidence: {x['bad_numbers'] or '-'}  judge: {x.get('judge', '-')}"
                          f"  {x.get('judge_reason', '')[:100]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-judge", action="store_true", help="skip the LLM judge (deterministic checks only, much faster)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N questions")
    ap.add_argument("--calibrate-only", action="store_true", help="only measure the judge against the human labels")
    a = ap.parse_args()
    rep = asyncio.run(run(not a.no_judge, a.limit, a.calibrate_only))
    print_report(rep)
    out = BACKEND_DIR / "var" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"quality_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    from .quality_report import write_html
    page = write_html(path)
    print(f"\nFull report: {Path(path).relative_to(BACKEND_DIR.parent)}\nReadable page: {Path(page).relative_to(BACKEND_DIR.parent)}")


if __name__ == "__main__":
    main()
