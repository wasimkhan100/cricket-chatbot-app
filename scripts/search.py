#!/usr/bin/env python3
"""Show search results for a question.  Usage: scripts/search.py "your question" [k] [--vector]"""
import asyncio
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"

CODE = r'''
import asyncio, sys
from cricket_chatbot.config import get_settings
from cricket_chatbot.llm.ollama import OllamaClient
from cricket_chatbot.rag.hybrid import season_of
from cricket_chatbot.rag.store import KnowledgeStore
from cricket_chatbot.resilience.health import HealthRegistry

async def main(q, k, mode):
    s = get_settings(); h = HealthRegistry()
    llm = OllamaClient(s.ollama_url, h, timeout_s=60, connect_timeout_s=3, embed_timeout_s=30, breaker_threshold=3, breaker_reset_s=5)
    store = KnowledgeStore(s.chroma_path, s.chroma_collection, llm, s.embed_model, h)
    hits = await store.search(q, k=k, max_distance=2.0, mode=mode)
    print(f"Question: {q}")
    if mode == "hybrid":
        store._load_index()
        types, ids = store._intent.preferred(q)
        print(f"Mode: hybrid  |  season filter: {season_of(q) or 'none'}  |  question type: {', '.join(sorted(types)) or 'general'}")
    else:
        print("Mode: plain vector search")
    print(f"Top {k} of {store.count()} documents (distance: 0 = same meaning; the chatbot uses <= 0.45, or a strong keyword match)\n")
    for rank, r in enumerate(hits, 1):
        used = r["distance"] <= 0.45 or r.get("keyword_rank", 99) <= 2
        kw = f"  keyword #{r['keyword_rank']}" if r.get("keyword_rank", 99) <= 30 and mode == "hybrid" and r["keyword_rank"] != 99 else ""
        print(f"{rank}. [{'USED' if used else 'skip'}] distance {r['distance']:.3f}{kw}  {r['id']}  ({r['type']})")
        print(f"   {r['text'][:220]}{'...' if len(r['text']) > 220 else ''}\n")
    await llm.close()

asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3]))
'''

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--vector"]
    mode = "vector" if "--vector" in sys.argv else "hybrid"
    q = args[0] if args else "What happens after a no ball?"
    k = args[1] if len(args) > 1 else "5"
    sys.exit(subprocess.call(["uv", "run", "python", "-c", CODE, q, k, mode], cwd=BACKEND))
