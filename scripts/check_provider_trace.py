"""用故障注入验证真实数据库和 HTTP Trace 的 Provider 逐次记录。"""

import asyncio
import json
import sys
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.config import Settings
from app.db.session import async_session_factory, engine
from app.models import Conversation, ConversationRun
from app.retrieval.hybrid import Evidence, RetrievalResult
from app.runtime import harness as harness_module
from app.runtime.harness import RuntimeHarness
from app.services.model_errors import ModelConnectionError
from app.services.model_gateway import ModelRouter
from app.services import conversation_service
from main import app


class FailingDeepSeek:
    async def generate(self, *args, **kwargs):
        raise ModelConnectionError("故障注入：主模型连接失败")


class WorkingOllama:
    async def generate(self, *args, **kwargs):
        return "申请条件请以学校公布的资料为准[1]。"


class EvidenceRetriever:
    def search(self, query, *, top_k):
        evidence = Evidence(
            chunk_id="trace:scholarship", source_key="trace", title="故障注入测试资料",
            url="https://example.test/trace", text="奖学金申请条件以学校公布的资料为准。",
            scope="测试", collection_status="ACTIVE", validity="UNKNOWN",
            version_label="trace-fixture", published_at="2026-01-01",
            score=1.0, vector_score=1.0, bm25_score=1.0,
        )
        return RetrievalResult(
            query=query, evidence=[evidence], conflict_detected=False, warnings=[]
        )


async def make_run(user_id: str):
    harness_module.model_gateway = ModelRouter(
        [("deepseek", FailingDeepSeek()), ("ollama", WorkingOllama())],
        Settings(model_provider="mock"),
    )

    async def skip_cache(*args, **kwargs):
        return None

    # 本次只验 Provider Trace；不让 Redis 的可用性影响结果。
    conversation_service.append_cached_exchange = skip_cache
    async with async_session_factory() as session:
        return await RuntimeHarness(retriever=EvidenceRetriever()).run(
            session,
            user_id=user_id,
            message="奖学金申请条件是什么？",
            conversation_id=None,
            grade="大二",
            college="计算机学院",
            major=None,
        )


async def cleanup(user_id: str):
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(ConversationRun).where(ConversationRun.user_id == user_id))
            await session.execute(delete(Conversation).where(Conversation.user_id == user_id))


async def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    user_id = "provider-trace-" + uuid.uuid4().hex[:12]
    try:
        result = await make_run(user_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get(f"/runs/{result.run_id}", params={"user_id": user_id})
        response.raise_for_status()
        trace = response.json()
        attempts = trace["model_attempts"]
        expected = [
            (1, "deepseek", "FAILED", "ModelConnectionError"),
            (2, "ollama", "SUCCEEDED", None),
        ]
        actual = [
            (item["attempt_no"], item["provider"], item["status"], item["error_code"])
            for item in attempts
        ]
        assert actual == expected, actual
        assert trace["status"] == "SUCCEEDED"
        assert all(item["operation"] == "answer" for item in attempts)
        print(json.dumps({
            "run_status": trace["status"],
            "attempts": attempts,
            "validation": "real MySQL + in-process HTTP + injected Provider failure",
        }, ensure_ascii=False))
    finally:
        await cleanup(user_id)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
