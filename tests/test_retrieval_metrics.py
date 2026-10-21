from types import SimpleNamespace

from scripts.evaluate_retrieval import evaluate


class FixedRetriever:
    def search(self, query, *, top_k, mode):
        ranked = {
            "two_relevant": ["a", "x", "y", "z", "w"],
            "one_relevant": ["x", "c", "y", "z", "w"],
        }[query]
        return SimpleNamespace(evidence=[SimpleNamespace(chunk_id=item) for item in ranked[:top_k]])


def test_recall_counts_each_relevant_chunk_not_only_any_hit() -> None:
    cases = [
        {"query": "two_relevant", "relevant": ["a", "b"]},
        {"query": "one_relevant", "relevant": ["c"]},
        {"query": "unanswerable", "relevant": []},
    ]
    report = evaluate(FixedRetriever(), "hybrid", cases)
    assert report["recall_at_5"] == 0.75  # (1/2 + 1/1) / 2
    assert report["mrr"] == 0.75  # (1/1 + 1/2) / 2
    assert report["answerable_queries"] == 2
    assert report["unanswerable_queries"] == 1
    assert report["queries"] == 3
