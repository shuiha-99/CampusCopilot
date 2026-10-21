"""Verify real local model usage survives MySQL persistence and the Trace API."""

import asyncio
import json
import sys
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import async_session_factory, engine
from app.models import Conversation, ConversationRun
from app.retrieval.hybrid import Evidence, RetrievalResult
from app.runtime import harness as harness_module
from app.runtime.harness import RuntimeHarness
from app.services.model_gateway import OllamaGateway
from main import app


EVIDENCE = Evidence(
    chunk_id="token-check:source", source_key="token-check", title="合成测试资料",
    url="https://example.test/token-check",
    text="奖助申请需核对当年通知与适用范围。",
    scope="合成测试", collection_status="ACTIVE", validity="UNKNOWN",
    version_label="token-check-v1", published_at="2025-01-01", score=1.0,
    vector_score=1.0, bm25_score=1.0,
)


class FixedRetriever:
    def search(self, query, *, top_k):
        return RetrievalResult(
            query=query, evidence=[EVIDENCE], conflict_detected=False, warnings=[]
        )


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    user_id = "token-trace-" + uuid4().hex[:10]
    gateway = OllamaGateway(settings, classification_enabled=False)
    previous = harness_module.model_gateway
    harness_module.model_gateway = gateway
    try:
        async with async_session_factory() as session:
            result = await RuntimeHarness(retriever=FixedRetriever()).run(
                session, user_id=user_id,
                message="奖助申请为什么需要核对当年通知与适用范围？",
                conversation_id=None,
                grade="大二", college="计算机学院", major=None,
            )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(
                f"/runs/{result.run_id}", params={"user_id": user_id}
            )
        response.raise_for_status()
        attempts = response.json()["model_attempts"]
        assert len(attempts) == 1, attempts
        attempt = attempts[0]
        assert attempt["provider"] == "ollama" and attempt["status"] == "SUCCEEDED"
        assert attempt["prompt_tokens"] > 0 and attempt["completion_tokens"] > 0
        assert attempt["total_tokens"] == attempt["prompt_tokens"] + attempt["completion_tokens"]
        print(json.dumps({
            "run_id": result.run_id,
            "provider": attempt["provider"],
            "prompt_tokens": attempt["prompt_tokens"],
            "completion_tokens": attempt["completion_tokens"],
            "total_tokens": attempt["total_tokens"],
            "trace_readback": True,
        }, ensure_ascii=False))
    finally:
        harness_module.model_gateway = previous
        await gateway._client.close()
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ConversationRun).where(ConversationRun.user_id == user_id))
                await session.execute(delete(Conversation).where(Conversation.user_id == user_id))
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
