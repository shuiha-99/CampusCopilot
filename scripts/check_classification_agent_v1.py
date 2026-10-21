"""Verify structured classification through real MySQL and in-process HTTP Trace."""

import asyncio
import json
import sys
import uuid
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import delete

from app.cache.conversation_cache import build_history_key
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory
from app.models import Conversation, ConversationRun
from app.runtime.harness import runtime_harness
from main import app


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    user_id = "classification-v1-" + uuid.uuid4().hex[:12]
    conversation_id = None
    try:
        async with async_session_factory() as session:
            result = await runtime_harness.run(
                session,
                user_id=user_id,
                message="请帮我创建奖学金准备清单，我是大二计算机工程学院学生，想了解申请条件和流程。",
                conversation_id=None,
                grade="大二",
                college="计算机工程学院",
                major=None,
            )
        conversation_id = result.conversation_id
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get(f"/runs/{result.run_id}", params={"user_id": user_id})
        response.raise_for_status()
        trace = response.json()
        board = trace["blackboard"]
        operations = [item["operation"] for item in trace["model_attempts"]]
        assert "classification" in operations and "answer" in operations, operations
        assert result.risk_level == "HIGH"
        assert trace["risk_level"] == "HIGH"
        report = {
            "environment": "real MySQL, Ollama classification and answer, in-process HTTP Trace",
            "run_status": trace["status"],
            "classification": board["classification"],
            "effective_intent": result.intent,
            "effective_risk_level": result.risk_level,
            "model_attempts": [
                {key: item[key] for key in ("attempt_no", "provider", "operation", "status", "error_code")}
                for item in trace["model_attempts"]
            ],
            "citation_count": len(result.citations),
            "can_create_task": result.can_create_task,
            "action_created": False,
        }
        output = Path("data/evaluation/classification_agent_v1_ollama.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ConversationRun).where(ConversationRun.user_id == user_id))
                await session.execute(delete(Conversation).where(Conversation.user_id == user_id))
        if conversation_id is not None:
            try:
                await redis_client.delete(build_history_key(user_id, conversation_id))
            except RedisError:
                pass
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
