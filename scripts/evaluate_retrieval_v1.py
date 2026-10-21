"""Evaluate the versioned development set without touching the production index."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import statistics
import time
from pathlib import Path

from app.core.config import settings
from app.retrieval import HybridRetriever


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "evaluation" / "v1"
MODES = ("bm25", "vector", "hybrid")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(path: Path = DATASET) -> tuple[list[dict], list[dict], dict]:
    source_file = path / "sources.json"
    case_file = path / "cases.json"
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, file in (("sources_sha256", source_file), ("cases_sha256", case_file)):
        if manifest[name] != sha256(file):
            raise ValueError(f"{file} changed: publish a new dataset version and rerun")
    sources = json.loads(source_file.read_text(encoding="utf-8"))
    cases = json.loads(case_file.read_text(encoding="utf-8"))
    ids = [f"{source['source_key']}:{chunk['chunk_key']}"
           for source in sources for chunk in source["chunks"]]
    if len(ids) != len(set(ids)) or len(cases) != manifest["case_count"]:
        raise ValueError("duplicate chunk ID or unexpected case count")
    if len(ids) != manifest["chunk_count"] or len(sources) != manifest["source_count"]:
        raise ValueError("source snapshot count changed")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("duplicate case ID")
    for case in cases:
        relevant = case["relevant"]
        if len(relevant) != len(set(relevant)) or not set(relevant) <= set(ids):
            raise ValueError(f"invalid relevant chunks: {case['id']}")
    return sources, cases, manifest


def percentile_nearest_rank(values: list[float], quantile: float) -> float:
    return sorted(values)[math.ceil(quantile * len(values)) - 1]


def evaluate_mode(retriever: HybridRetriever, cases: list[dict], mode: str) -> dict:
    recall_sum = 0.0
    reciprocal_rank_sum = 0.0
    answerable = 0
    unanswerable = 0
    unanswerable_without_evidence = 0
    errors = []
    durations = []
    details = []
    unanswerable_details = []
    for warmup_case in cases[:3]:
        retriever._vector_score_cache.clear()
        retriever.search(warmup_case["query"], top_k=5, mode=mode)
    for case in cases:
        # Include embedding and Chroma query work in each timing sample.
        # Avoid reusing query-vector results from metric runs or other modes.
        retriever._vector_score_cache.clear()
        started = time.perf_counter()
        try:
            result = retriever.search(case["query"], top_k=5, mode=mode)
        except Exception as exc:  # preserve errors in the benchmark report
            errors.append({"id": case["id"], "type": type(exc).__name__})
            continue
        durations.append((time.perf_counter() - started) * 1000)
        ranked = [item.chunk_id for item in result.evidence]
        relevant = set(case["relevant"])
        if relevant:
            answerable += 1
            found = relevant.intersection(ranked)
            recall = len(found) / len(relevant)
            ranks = [ranked.index(chunk_id) + 1 for chunk_id in found]
            reciprocal_rank = 1 / min(ranks) if ranks else 0.0
            recall_sum += recall
            reciprocal_rank_sum += reciprocal_rank
            details.append({"id": case["id"], "relevant_count": len(relevant),
                            "found_count": len(found), "first_relevant_rank": min(ranks) if ranks else None})
        else:
            unanswerable += 1
            unanswerable_without_evidence += not ranked
            unanswerable_details.append({"id": case["id"], "returned_chunk_ids": ranked})
    return {
        "attempted_queries": len(cases),
        "completed_queries": len(durations),
        "errors": errors,
        "error_rate": round(len(errors) / len(cases), 4),
        "answerable_queries_completed": answerable,
        "unanswerable_queries_completed": unanswerable,
        "recall_at_5": round(recall_sum / answerable, 4) if answerable else None,
        "mrr": round(reciprocal_rank_sum / answerable, 4) if answerable else None,
        "unanswerable_without_retrieved_evidence": unanswerable_without_evidence,
        "latency_ms": {
            "samples": len(durations),
            "p95_nearest_rank": round(percentile_nearest_rank(durations, 0.95), 2) if durations else None,
            "median": round(statistics.median(durations), 2) if durations else None,
            "max": round(max(durations), 2) if durations else None,
        },
        "cases": details,
        "unanswerable_cases": unanswerable_details,
    }


def main() -> None:
    sources, cases, manifest = load_dataset()
    chroma_path = DATASET / ("chroma_" + settings.embedding_provider)
    retriever = HybridRetriever(source_file=DATASET / "sources.json",
                                chroma_path=chroma_path, embedding_keep_alive="5m")
    indexing_started = time.perf_counter()
    retriever.index()
    indexing_ms = (time.perf_counter() - indexing_started) * 1000
    report = {
        "dataset_version": manifest["version"],
        "dataset_role": manifest["role"],
        "sources_sha256": manifest["sources_sha256"],
        "cases_sha256": manifest["cases_sha256"],
        "source_count": len(sources),
        "chunk_count": manifest["chunk_count"],
        "case_count": len(cases),
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.ollama_embedding_model if settings.embedding_provider == "ollama" else "HashingEmbedding-384",
        "environment": {"os": platform.platform(), "python": platform.python_version(),
                        "index_path": str(chroma_path), "indexing_ms": round(indexing_ms, 2),
                        "query_order": "case order for each mode: bm25, vector, hybrid", "warmup": "first three queries before each mode, excluded from latency samples",
                        "cache": "query vector cache cleared before each sample",
                        "latency_scope": "retriever.search only; excludes indexing, answer generation and HTTP"},
        "modes": {mode: evaluate_mode(retriever, cases, mode) for mode in MODES},
        "note": "No-evidence count is retrieval behavior, not a generation refusal test. This set is used for development/regression, not independent test.",
    }
    output = DATASET / f"results_{settings.embedding_provider}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "modes": {mode: {
        key: value for key, value in values.items() if key not in {"cases", "errors"}}
        for mode, values in report["modes"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
