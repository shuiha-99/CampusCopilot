"""Run v1's three no-answer questions through the Agent HTTP and Trace path."""

import asyncio
import json
import sys

from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import delete, select

from app.api import agent as agent_module
from app.cache.conversation_cache import build_history_key
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory, engine
from app.models import Conversation, ConversationRun
from app.retrieval import HybridRetriever
from app.runtime.harness import RuntimeHarness
from main import app
from scripts.evaluate_retrieval_v1 import DATASET, load_dataset


USER_ID = "evaluation-v1-unanswerable"


async def cleanup() -> None:
    async with async_session_factory() as session:
        async with session.begin():
            conversation_ids = list((await session.scalars(
                select(Conversation.id).where(Conversation.user_id == USER_ID)
            )).all())
            await session.execute(delete(ConversationRun).where(ConversationRun.user_id == USER_ID))
            await session.execute(delete(Conversation).where(Conversation.user_id == USER_ID))
    for conversation_id in conversation_ids:
        try:
            await redis_client.delete(build_history_key(USER_ID, conversation_id))
        except RedisError:
            pass
    await engine.dispose()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _, cases, manifest = load_dataset()
    retriever = HybridRetriever(
        source_file=DATASET / "sources.json",
        chroma_path=DATASET / "chroma_ollama",
        embedding_keep_alive="5m",
    )
    previous_harness = agent_module.runtime_harness
    agent_module.runtime_harness = RuntimeHarness(retriever=retriever)
    asyncio.run(cleanup())
    rows = []
    try:
        with TestClient(app) as client:
            for case in cases:
                if case["relevant"]:
                    continue
                response = client.post("/agent/query", json={"user_id": USER_ID, "message": case["query"]})
                if response.status_code != 200:
                    raise AssertionError(f"{case['id']}: HTTP {response.status_code} {response.text}")
                answer = response.json()
                trace_response = client.get(f"/runs/{answer['run_id']}", params={"user_id": USER_ID})
                if trace_response.status_code != 200:
                    raise AssertionError(f"{case['id']}: trace HTTP {trace_response.status_code}")
                trace = trace_response.json()
                raw_count = len(trace["retrievals"][0]["results"])
                if (
                    "没有找到足够证据" not in answer["answer"]
                    or answer["citations"]
                    or answer["can_create_task"]
                    or trace["model_attempts"]
                    or trace["status"] != "SUCCEEDED"
                ):
                    raise AssertionError(f"{case['id']}: unsafe answer or incomplete trace")
                rows.append({"id": case["id"], "raw_retrieved": raw_count,
                             "answer_citations": len(answer["citations"]),
                             "model_attempts": len(trace["model_attempts"]),
                             "run_status": trace["status"], "answer": answer["answer"]})
    finally:
        agent_module.runtime_harness = previous_harness
        asyncio.run(cleanup())
    report = {"dataset_version": manifest["version"],
              "scope": "real Ollama embedding and MySQL with in-process HTTP; model generation is intentionally skipped by evidence guards",
              "cases": rows}
    output = DATASET / "unanswerable_agent_check.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
