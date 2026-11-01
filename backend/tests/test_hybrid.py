import numpy as np

from cricket_chatbot.rag.hybrid import BM25Index, IndexedDoc, Intent, expand_query, mmr, rrf, season_of, tokenize


def docs():
    return [
        IndexedDoc("rule-super-over", "Super over. If a T20 match is tied, a super over decides it.", {"type": "rule"}),
        IndexedDoc("season-2016", "IPL 2016 season: Champion: Sunrisers Hyderabad.", {"type": "season", "season": 2016}),
        IndexedDoc("match-636", "IPL 2016 FINAL, the title decider: Sunrisers Hyderabad became champions.", {"type": "match", "season": 2016, "stage": "final"}),
        IndexedDoc("match-600", "IPL 2016 match: Mumbai Indians won by 6 wickets.", {"type": "match", "season": 2016, "stage": "league"}),
        IndexedDoc("player-v-kohli", "V Kohli played IPL cricket for Royal Challengers Bangalore.", {"type": "player"}),
        IndexedDoc("team-mumbai-indians", "Mumbai Indians played 157 IPL matches. Titles: 2013, 2015, 2017.", {"type": "team"}),
    ]


def test_tokenize_and_expand():
    assert tokenize("Who won the Titles?") == ["won", "title"]
    assert tokenize("sixes batches wickets") == ["six", "batch", "wicket"]
    assert "champion" in expand_query("Who won in 2016?")
    assert "duckworth" in expand_query("rain rules")
    assert season_of("final of 2016") == 2016 and season_of("in 2023") is None


def test_bm25_prefers_exact_terms_and_respects_filter():
    d = docs()
    idx = BM25Index(d, np.eye(len(d), dtype=np.float32))
    top = idx.search(expand_query("2016 final"), None, 3)
    assert d[top[0][0]].id == "match-636"
    only_rules = {0}
    assert all(i in only_rules for i, _ in idx.search(["super", "over"], only_rules, 5))


def test_rrf_rewards_agreement():
    fused = rrf([[1, 2, 3], [3, 1, 2]], [1.0, 1.0])
    assert max(fused, key=fused.get) == 1


def test_mmr_skips_near_duplicates():
    emb = np.array([[1, 0], [0.999, 0.045], [0, 1]], dtype=np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    rel = {0: 1.0, 1: 0.99, 2: 0.8}   # a near-copy of the top hit loses to a different, slightly less relevant doc
    assert mmr([0, 1, 2], rel, emb, 2) == [0, 2]


def test_intent_routing():
    it = Intent(docs())
    assert it.preferred("How good is Kohli?") == ({"player"}, {"player-v-kohli"})
    assert "team" in it.preferred("Mumbai Indians titles history")[0]
    assert it.preferred("Who won in 2016?")[0] == {"season", "final"}
    assert it.preferred("What happens if the match is tied?")[0] == {"rule"}
