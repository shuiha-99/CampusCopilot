import hashlib
import json
from pathlib import Path

from app.core.config import settings
from app.retrieval import HybridRetriever


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def evaluate(
    retriever: HybridRetriever, mode: str, cases: list[dict], *, k: int = 5
) -> dict[str, float | int]:
    recall_sum = 0.0
    reciprocal_rank = 0.0
    answerable_count = 0
    unanswerable_count = 0
    for case in cases:
        relevant = set(case["relevant"])
        if not relevant:
            # 无相关块问题需单独检查拒答，不进入Recall/MRR分母。
            unanswerable_count += 1
            continue
        answerable_count += 1
        result = retriever.search(case["query"], top_k=k, mode=mode)
        ranked = [item.chunk_id for item in result.evidence]
        found = relevant.intersection(ranked)
        recall_sum += len(found) / len(relevant)
        ranks = [ranked.index(chunk_id) + 1 for chunk_id in found]
        if ranks:
            reciprocal_rank += 1.0 / min(ranks)
    return {
        "queries": len(cases),
        "answerable_queries": answerable_count,
        "unanswerable_queries": unanswerable_count,
        f"recall_at_{k}": round(recall_sum / answerable_count, 4) if answerable_count else 0.0,
        "mrr": round(reciprocal_rank / answerable_count, 4) if answerable_count else 0.0,
    }


def main() -> None:
    question_file = PROJECT_ROOT / "data" / "evaluation" / "retrieval_queries.json"
    source_file = PROJECT_ROOT / "data" / "knowledge" / "sources.json"
    question_bytes = question_file.read_bytes()
    cases = json.loads(question_bytes.decode("utf-8"))
    # Reuse both Chroma and per-query vector results across modes/splits. During
    # this standalone benchmark bge-m3 may remain resident briefly; the normal
    # application path still unloads it after each request so Qwen 7B can fit.
    retriever = HybridRetriever(embedding_keep_alive="5m")
    report = {
        split: {
            mode: evaluate(
                retriever, mode, [case for case in cases if case["split"] == split]
            )
            for mode in ("vector", "bm25", "hybrid")
        }
        for split in ("tune", "holdout")
    }
    report["all"] = {
        mode: evaluate(retriever, mode, cases)
        for mode in ("vector", "bm25", "hybrid")
    }
    # 旧26题只供历史对照；新版冻结集另行发布版本，不能混作最终成绩。
    report["dataset_status"] = "legacy_exploratory_not_frozen"
    report["embedding_provider"] = settings.embedding_provider
    report["questions_sha256"] = hashlib.sha256(question_bytes).hexdigest()
    report["sources_sha256"] = hashlib.sha256(source_file.read_bytes()).hexdigest()
    output = PROJECT_ROOT / "data" / "evaluation" / "legacy_corrected_results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
