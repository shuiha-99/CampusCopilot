"""Inspect conservative Agent evidence guards on all v1 development cases."""

import json

from app.core.config import settings
from app.retrieval import HybridRetriever
from app.runtime.harness import MIN_QUERY_BIGRAM_COVERAGE, RuntimeHarness
from scripts.evaluate_retrieval_v1 import DATASET, load_dataset


def main() -> None:
    _, cases, manifest = load_dataset()
    retriever = HybridRetriever(
        source_file=DATASET / "sources.json",
        chroma_path=DATASET / ("chroma_" + settings.embedding_provider),
        embedding_keep_alive="5m",
    )
    rows = []
    for case in cases:
        raw = retriever.search(case["query"], top_k=5).evidence
        year_filtered = RuntimeHarness._evidence_for_requested_year(case["query"], raw)
        supporting = RuntimeHarness._evidence_with_query_coverage(case["query"], year_filtered)
        rows.append({
            "id": case["id"],
            "label_has_relevant_chunk": bool(case["relevant"]),
            "raw_retrieved": len(raw),
            "after_year_check": len(year_filtered),
            "after_topic_check": len(supporting),
        })
    positive = sum(row["label_has_relevant_chunk"] for row in rows)
    negative = len(rows) - positive
    false_positive = [row["id"] for row in rows if not row["label_has_relevant_chunk"] and row["after_topic_check"]]
    false_negative = [row["id"] for row in rows if row["label_has_relevant_chunk"] and not row["after_topic_check"]]
    report = {
        "dataset_version": manifest["version"],
        "dataset_role": manifest["role"],
        "embedding_provider": settings.embedding_provider,
        "minimum_query_bigram_coverage": MIN_QUERY_BIGRAM_COVERAGE,
        "label_positive": positive,
        "label_negative": negative,
        "false_positive_ids": false_positive,
        "false_negative_ids": false_negative,
        "cases": rows,
        "note": "Threshold inspected on this same development set. Passing it does not prove factual entailment, independent generalization or model refusal.",
    }
    output = DATASET / f"answerability_guard_results_{settings.embedding_provider}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
