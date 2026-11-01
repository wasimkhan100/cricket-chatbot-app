"""Chroma vector store. Embeddings come from Ollama (nomic-embed-text) so the whole stack stays local."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import chromadb
import numpy as np

from ..llm.ollama import LLMUnavailable, OllamaClient
from ..observability.metrics import RAG_HITS
from ..observability.tracing import tracer
from ..resilience.circuit_breaker import CircuitOpenError
from ..resilience.health import HealthRegistry
from .documents import Doc
from .hybrid import BM25Index, IndexedDoc, Intent, expand_query, mmr, rrf, season_of

log = logging.getLogger(__name__)

# nomic-embed-text is trained with task prefixes; using them noticeably improves retrieval.
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class KnowledgeUnavailable(Exception):
    pass


class KnowledgeStore:
    def __init__(self, path: Path, collection: str, llm: OllamaClient, embed_model: str, health: HealthRegistry,
                 breaker_threshold: int = 3, breaker_reset_s: float = 20.0):
        self.path = path
        self.collection_name = collection
        self.llm = llm
        self.embed_model = embed_model
        self.health = health
        self.breaker = health.breaker("chroma", failure_threshold=breaker_threshold, reset_after_s=breaker_reset_s)
        self._client = None
        self._col = None
        self._index: BM25Index | None = None
        self._intent: Intent | None = None

    def _collection(self):
        if self._col is None:
            self.path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.path), settings=chromadb.Settings(anonymized_telemetry=False))
            self._col = self._client.get_or_create_collection(self.collection_name, embedding_function=None,
                                                              metadata={"hnsw:space": "cosine"})
        return self._col

    def count(self) -> int:
        try:
            return self._collection().count()
        except Exception:
            return 0

    async def ingest(self, docs: list[Doc], batch_size: int = 32) -> dict:
        """Upserts only documents whose text changed since the last run (content hash in metadata)."""
        col = self._collection()
        existing = {}
        got = col.get(include=["metadatas"])
        for i, m in zip(got["ids"], got["metadatas"]):
            existing[i] = (m or {}).get("hash")
        todo = []
        for d in docs:
            h = hashlib.sha256(d.text.encode()).hexdigest()[:16]
            if existing.get(d.id) != h:
                todo.append((d, h))
        stale = set(existing) - {d.id for d in docs}
        if stale:
            col.delete(ids=list(stale))
        for start in range(0, len(todo), batch_size):
            batch = todo[start:start + batch_size]
            vecs = await self.llm.embed([DOC_PREFIX + d.text for d, _ in batch], self.embed_model)
            col.upsert(ids=[d.id for d, _ in batch], documents=[d.text for d, _ in batch], embeddings=vecs,
                       metadatas=[{**d.meta, "hash": h} for d, h in batch])
            log.info("ingested batch", extra={"done": start + len(batch), "total": len(todo)})
        self._index = None  # rebuilt on next search
        return {"total": len(docs), "embedded": len(todo), "deleted": len(stale), "count": col.count()}

    # --- hybrid index -------------------------------------------------------------
    def _load_index(self) -> BM25Index:
        col = self._collection()
        n = col.count()
        if self._index is None or len(self._index) != n:
            got = col.get(include=["documents", "metadatas", "embeddings"])
            docs = [IndexedDoc(i, d, m or {}) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])]
            emb = np.asarray(got["embeddings"], dtype=np.float32) if docs else np.zeros((0, 1), dtype=np.float32)
            self._index = BM25Index(docs, emb)
            self._intent = Intent(docs)
            log.info("keyword index built", extra={"docs": len(docs)})
        return self._index

    async def search(self, query: str, k: int = 4, where: dict | None = None, max_distance: float = 0.6,
                     mode: str = "hybrid") -> list[dict]:
        """mode="hybrid": season filter + BM25 and vector ranks fused (RRF) + MMR diversity.
        mode="vector": plain nearest-neighbour search, kept for comparison (rag/evaluate.py)."""
        with tracer().start_as_current_span("rag.search") as span:
            span.set_attribute("rag.k", k)
            span.set_attribute("rag.mode", mode)
            try:
                vec = (await self.llm.embed([QUERY_PREFIX + query], self.embed_model))[0]
            except LLMUnavailable as e:
                raise KnowledgeUnavailable(str(e)) from e

            async def go():
                self.health.check_chaos("chroma")
                if mode == "vector":
                    col = self._collection()
                    if col.count() == 0:
                        return []
                    res = await asyncio.to_thread(col.query, query_embeddings=[vec], n_results=k, where=where,
                                                  include=["documents", "metadatas", "distances"])
                    return [{"id": i, "text": d, "type": (m or {}).get("type"), "distance": round(dist, 3)}
                            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])]
                return await asyncio.to_thread(self._hybrid, query, vec, k, where, span)

            try:
                hits = await self.breaker.call(go)
            except CircuitOpenError as e:
                raise KnowledgeUnavailable(str(e)) from e
            except Exception as e:
                raise KnowledgeUnavailable(f"vector search failed: {e}") from e
            hits = [h for h in hits if h["distance"] <= max_distance or h.get("keyword_rank", 99) <= 2]
            RAG_HITS.observe(len(hits))
            span.set_attribute("rag.hits", len(hits))
            span.set_attribute("rag.ids", json.dumps([h["id"] for h in hits]))
            return hits

    def _hybrid(self, query: str, vec: list[float], k: int, where: dict | None, span) -> list[dict]:
        idx = self._load_index()
        if len(idx) == 0:
            return []  # nothing ingested yet: no hits, not an error
        col = self._collection()
        pool = max(30, k * 6)

        # 1. Season filter: a question naming a year only searches that season (falls back if nothing matches).
        season = season_of(query)
        filt = where or ({"season": season} if season else None)
        allowed = {i for i, d in enumerate(idx.docs) if all(d.meta.get(fk) == fv for fk, fv in filt.items())} if filt else None
        if allowed is not None and not allowed:
            filt, allowed = where, None
        span.set_attribute("rag.season_filter", season or 0)

        # 2. Two rankings: vector (Chroma HNSW) and keyword (BM25).
        res = col.query(query_embeddings=[vec], n_results=min(pool, len(idx)), where=filt, include=["distances"])
        vec_rank = [idx.pos[i] for i in res["ids"][0] if i in idx.pos]
        kw = idx.search(expand_query(query), allowed, pool)
        kw_rank = [i for i, _ in kw]
        kw_pos = {i: r for r, i in enumerate(kw_rank)}
        top_kw = kw[0][1] if kw else 0.0

        # 3. Intent ranking: documents of the type the question is about, ordered by vector similarity.
        q = np.asarray(vec, dtype=np.float32)
        q /= np.linalg.norm(q) or 1.0
        types, wanted_ids = self._intent.preferred(query)
        sims = idx.emb @ q

        def preferred(i: int) -> bool:
            d = idx.docs[i]
            return (d.id in wanted_ids or d.meta.get("type") in types or ("final" in types and d.meta.get("stage") == "final")) \
                and (allowed is None or i in allowed or d.meta.get("type") in ("player", "team", "rule"))
        intent_rank = sorted((i for i in range(len(idx)) if preferred(i)), key=lambda i: (idx.docs[i].id not in wanted_ids, -sims[i]))[:pool]
        span.set_attribute("rag.intent", ",".join(sorted(types)) or "none")

        # 4. Fuse the three rankings, then 5. diversify.
        fused = rrf([vec_rank, kw_rank, intent_rank], [1.0, 0.7, 1.2])
        candidates = sorted(fused, key=fused.get, reverse=True)[:pool]
        picked = mmr(candidates, fused, idx.emb, k)

        out = []
        for i in picked:
            d = idx.docs[i]
            kr = kw_pos.get(i)
            strong_kw = kr is not None and kw[kr][1] >= 0.5 * top_kw
            out.append({"id": d.id, "text": d.text, "type": d.meta.get("type"),
                        "distance": round(1 - float(idx.emb[i] @ q), 3), "score": round(fused[i], 4),
                        "keyword_rank": kr + 1 if strong_kw else 99})
        return out
