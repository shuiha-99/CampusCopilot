"""Probe model answers for the three no-evidence cases in the v1 snapshot."""

import asyncio
import json
from pathlib import Path

from app.core.config import settings
from app.retrieval import HybridRetriever
from app.runtime.harness import RuntimeHarness
from app.services.model_gateway import model_gateway
from scripts.evaluate_retrieval_v1 import DATASET, load_dataset


async def main() -> None:
    _, cases, manifest = load_dataset()
    retriever = HybridRetriever(
        source_file=DATASET / "sources.json",
        chroma_path=DATASET / ("chroma_" + settings.embedding_provider),
        embedding_keep_alive="5m",
    )
    retriever.index()
    results = []
    for case in cases:
        if case["relevant"]:
            continue
        retrieved = retriever.search(case["query"], top_k=5, mode="hybrid")
        context = RuntimeHarness._evidence_context(retrieved.evidence, None)
        try:
            answer = await model_gateway.generate(case["query"], history=[], context=context)
            error = None
        except Exception as exc:
            answer = None
            error = type(exc).__name__
        results.append({"id": case["id"], "query": case["query"],
                        "returned_chunk_ids": [item.chunk_id for item in retrieved.evidence],
                        "answer": answer, "error": error})
    report = {"dataset_version": manifest["version"],
              "dataset_role": manifest["role"],
              "embedding_provider": settings.embedding_provider,
              "model_provider_config": settings.model_provider,
              "scope": "real model generation with the same v1 retrieval context; no HTTP, database, conversation history, or full Agent run",
              "cases": results}
    output = DATASET / "unanswerable_model_probe.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
