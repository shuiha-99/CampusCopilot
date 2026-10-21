import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.evaluate_retrieval_v1 import evaluate_mode, load_dataset, percentile_nearest_rank


class FixedRetriever:
    def __init__(self):
        self._vector_score_cache = {}

    def search(self, query, *, top_k, mode):
        ranked = {
            "many": ["a", "b", "c", "d", "e"],
            "none": ["irrelevant"],
        }[query]
        return SimpleNamespace(evidence=[SimpleNamespace(chunk_id=id) for id in ranked[:top_k]])


def test_more_than_five_relevant_chunks_limit_recall_and_unanswerable_is_separate() -> None:
    cases = [
        {"id": "a", "query": "many", "relevant": ["a", "b", "c", "d", "e", "f"]},
        {"id": "b", "query": "none", "relevant": []},
    ]
    report = evaluate_mode(FixedRetriever(), cases, "hybrid")
    assert report["recall_at_5"] == 0.8333
    assert report["mrr"] == 1.0
    assert report["answerable_queries_completed"] == 1
    assert report["unanswerable_queries_completed"] == 1
    assert report["unanswerable_without_retrieved_evidence"] == 0
    assert percentile_nearest_rank([1, 2, 3, 4, 5], 0.95) == 5


def test_manifest_rejects_modified_sources(tmp_path: Path) -> None:
    dataset = Path(__file__).resolve().parents[1] / "data" / "evaluation" / "v1"
    for filename in ("sources.json", "cases.json", "manifest.json"):
        (tmp_path / filename).write_bytes((dataset / filename).read_bytes())
    sources, cases, _ = load_dataset(tmp_path)
    assert len(sources) == 6 and len(cases) == 39
    source_file = tmp_path / "sources.json"
    sources[0]["chunks"][0]["text"] += " changed"
    source_file.write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="publish a new dataset version"):
        load_dataset(tmp_path)
