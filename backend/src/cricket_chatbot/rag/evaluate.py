"""Retrieval evaluation: hit@1, hit@3, hit@5 and MRR over knowledge/retrieval_eval*.json.

Usage: uv run python -m cricket_chatbot.rag.evaluate [--mode vector|hybrid|both]
"""

import argparse
import asyncio
import json

from ..config import BACKEND_DIR, get_settings
from ..llm.ollama import OllamaClient
from ..resilience.health import HealthRegistry
from .store import KnowledgeStore


async def evaluate(mode: str, verbose: bool, file: str = "retrieval_eval.json") -> dict:
    s = get_settings()
    cases = json.loads((BACKEND_DIR / "knowledge" / file).read_text())
    h = HealthRegistry()
    llm = OllamaClient(s.ollama_url, h, timeout_s=60, connect_timeout_s=3, embed_timeout_s=30, breaker_threshold=5, breaker_reset_s=5)
    store = KnowledgeStore(s.chroma_path, s.chroma_collection, llm, s.embed_model, h)
    hits = {1: 0, 3: 0, 5: 0}
    rr = 0.0
    misses = []
    try:
        for c in cases:
            res = await store.search(c["q"], k=5, max_distance=2.0, mode=mode)
            ids = [r["id"] for r in res]
            rank = next((i + 1 for i, d in enumerate(ids) if d in c["relevant"]), None)
            for k in hits:
                hits[k] += bool(rank and rank <= k)
            rr += 1 / rank if rank else 0
            if not rank or rank > 1:
                misses.append((c["q"], rank, ids[:3]))
    finally:
        await llm.close()
    n = len(cases)
    out = {"set": file.removesuffix(".json"), "mode": mode, "questions": n, **{f"hit@{k}": round(v / n, 3) for k, v in hits.items()}, "mrr": round(rr / n, 3)}
    if verbose:
        for q, rank, top in misses:
            print(f"  [{mode}] rank {rank or '-'}: {q}  -> {top}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="both", choices=["vector", "hybrid", "both"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    # retrieval_eval.json was used while tuning; retrieval_eval_holdout.json was not, so it's the honest score.
    for file in ("retrieval_eval.json", "retrieval_eval_holdout.json"):
        for mode in (["vector", "hybrid"] if a.mode == "both" else [a.mode]):
            print(json.dumps(asyncio.run(evaluate(mode, a.verbose, file))))


if __name__ == "__main__":
    main()
