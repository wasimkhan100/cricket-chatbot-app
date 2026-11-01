"""Build the stats database and the vector index.  Usage:  uv run python -m cricket_chatbot.ingest [--rebuild-db]"""

import argparse
import asyncio
import logging
import time

from .config import BACKEND_DIR, get_settings
from .data.stats_db import StatsDB, build_database
from .llm.ollama import OllamaClient
from .observability.logging import setup_logging
from .rag.documents import build_all
from .rag.store import KnowledgeStore
from .resilience.health import HealthRegistry

log = logging.getLogger("ingest")


def ensure_database(rebuild: bool = False, settings=None) -> StatsDB:
    s = settings or get_settings()
    if rebuild or not s.sqlite_path.exists():
        t0 = time.time()
        build_database(s.data_dir, s.sqlite_path)
        log.info("stats database built", extra={"path": str(s.sqlite_path), "seconds": round(time.time() - t0, 1)})
    return StatsDB(s.sqlite_path)


async def ingest_knowledge(db: StatsDB) -> dict:
    s = get_settings()
    health = HealthRegistry()
    llm = OllamaClient(s.ollama_url, health, timeout_s=s.llm_timeout_s, connect_timeout_s=s.llm_connect_timeout_s,
                       embed_timeout_s=120, breaker_threshold=5, breaker_reset_s=5)
    try:
        store = KnowledgeStore(s.chroma_path, s.chroma_collection, llm, s.embed_model, health)
        docs = build_all(db, BACKEND_DIR / "knowledge" / "cricket_rules.md")
        log.info("documents built", extra={"count": len(docs)})
        return await store.ingest(docs)
    finally:
        await llm.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-db", action="store_true")
    args = ap.parse_args()
    setup_logging("INFO")
    db = ensure_database(args.rebuild_db)
    t0 = time.time()
    result = asyncio.run(ingest_knowledge(db))
    log.info("knowledge ingested", extra={**result, "seconds": round(time.time() - t0, 1)})


if __name__ == "__main__":
    main()
