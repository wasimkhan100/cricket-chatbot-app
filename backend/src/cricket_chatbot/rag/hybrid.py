"""Hybrid retrieval helpers: BM25 keyword index, query parsing, rank fusion and MMR diversity.

Why hybrid: embeddings capture the topic ("an IPL 2016 match result") but blur exact facts. 60 match summaries
from one season embed almost identically, so the word that matters ("final", a surname, a venue) gets lost.
BM25 scores those exact words; reciprocal rank fusion combines both rankings without tuning score scales.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

STOP = set("""a an the of in on at to for from by with and or is are was were be been it its this that these those
what which who whom whose how when where why do does did has have had can could would should will i me my you your
he his she her they them their we us our about tell show give ipl cricket match matches""".split())

# Query-side expansion: fans' words -> words the documents actually use.
SYNONYMS = {
    "won": "champion", "win": "champion", "winner": "champion", "winners": "champion", "trophy": "champion title",
    "title": "champion", "titles": "champion title", "champions": "champion",
    "rain": "duckworth lewis", "dls": "duckworth lewis", "tie": "super over", "tied": "super over", "level": "tied super over",
    "lbw": "leg before wicket", "review": "drs", "umpire": "drs",
    "top": "most", "highest": "most", "leading": "most", "charts": "most",
}

YEAR = re.compile(r"\b(20(?:0[89]|1[0-7]))\b")


def _stem(w: str) -> str:
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and w[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return w[:-2]  # sixes -> six, matches -> match (but titles -> title below)
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 1]


def expand_query(q: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", q.lower())
    extra = " ".join(SYNONYMS[w] for w in words if w in SYNONYMS)
    return tokenize(q + " " + extra)


def season_of(q: str) -> int | None:
    m = YEAR.search(q)
    return int(m.group(1)) if m else None


@dataclass
class IndexedDoc:
    id: str
    text: str
    meta: dict


class BM25Index:
    def __init__(self, docs: list[IndexedDoc], embeddings: np.ndarray, k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.pos = {d.id: i for i, d in enumerate(docs)}
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d.text)) for d in docs]
        self.len = np.array([sum(t.values()) for t in self.tf], dtype=float)
        self.avg = float(self.len.mean()) if len(docs) else 1.0
        df = Counter(w for t in self.tf for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        self.emb = embeddings / np.where(norms == 0, 1, norms)  # unit vectors: dot product = cosine similarity

    def __len__(self) -> int:
        return len(self.docs)

    def search(self, terms: list[str], allowed: set[int] | None, top: int) -> list[tuple[int, float]]:
        scores = np.zeros(len(self.docs))
        for w in set(terms):
            idf = self.idf.get(w)
            if not idf:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(w)
                if f:
                    scores[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        order = np.argsort(-scores)
        out = [(int(i), float(scores[i])) for i in order if scores[i] > 0 and (allowed is None or int(i) in allowed)]
        return out[:top]


RULE_CUES = re.compile(r"\b(what is|what's a|what are|how does|how do|how many|rule|rules|explain|allowed|mean|means|"
                       r"when is|why is|definition|happens (after|if|when))\b", re.I)
SEASON_CUES = re.compile(r"\b(won|win|winner|winners|champion|champions|title|final|orange|purple|cap|most|top|"
                         r"highest|leading|season|run charts)\b", re.I)
TEAM_CUES = re.compile(r"\b(history|titles?|trophy|trophies|ever|record|franchise|how many times)\b", re.I)


class Intent:
    """Which document types a question is most likely about, from cheap cues. Used as a third ranking."""

    def __init__(self, docs: list[IndexedDoc]):
        self.player_names: dict[str, set[str]] = {}
        self.team_names: dict[str, str] = {}
        for d in docs:
            t = d.meta.get("type")
            if t in ("player", "team"):
                name = d.text.split(" played")[0].strip()
                if t == "team":
                    self.team_names[name.lower()] = d.id
                else:
                    for part in {name.lower(), name.lower().split()[-1]}:
                        if len(part) >= 4:
                            self.player_names.setdefault(part, set()).add(d.id)

    def preferred(self, q: str) -> tuple[set[str], set[str]]:
        """Returns (preferred types, specific doc ids)."""
        low = q.lower()
        types: set[str] = set()
        ids: set[str] = set()
        has_year = bool(YEAR.search(q))
        for name, doc_ids in self.player_names.items():
            if re.search(rf"\b{re.escape(name)}\b", low):
                ids |= doc_ids
        if ids:
            types.add("player")
        teams = [doc_id for name, doc_id in self.team_names.items() if name in low]
        if teams and not has_year and TEAM_CUES.search(q):
            types.add("team")
            ids |= set(teams)
        if has_year and SEASON_CUES.search(q):
            types |= {"season", "final"}
        if RULE_CUES.search(q) and not has_year and not ids:
            types.add("rule")
        return types, ids


def rrf(rankings: list[list[int]], weights: list[float], k: int = 60) -> dict[int, float]:
    """Reciprocal rank fusion: robust to the two retrievers' very different score scales."""
    fused: dict[int, float] = {}
    for ranking, w in zip(rankings, weights):
        for rank, idx in enumerate(ranking):
            fused[idx] = fused.get(idx, 0.0) + w / (k + rank + 1)
    return fused


def mmr(candidates: list[int], relevance: dict[int, float], emb: np.ndarray, k: int, lam: float = 0.75) -> list[int]:
    """Maximal marginal relevance: each pick balances relevance against similarity to what's already picked."""
    if not candidates:
        return []
    top = max(relevance[c] for c in candidates) or 1.0
    chosen: list[int] = []
    pool = list(candidates)
    while pool and len(chosen) < k:
        def score(c: int) -> float:
            redundancy = max((float(emb[c] @ emb[s]) for s in chosen), default=0.0)
            return lam * relevance[c] / top - (1 - lam) * redundancy
        best = max(pool, key=score)
        chosen.append(best)
        pool.remove(best)
    return chosen
