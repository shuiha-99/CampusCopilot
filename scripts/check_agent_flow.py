import asyncio
import json
import sys

from sqlalchemy import delete, select
from redis.exceptions import RedisError

from app.cache.conversation_cache import build_history_key
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory
from app.models import Conversation, ConversationRun, ModelAttempt, RetrievalRun
from app.runtime.harness import runtime_harness


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def cleanup(user_id: str) -> None:
    conversation_ids: list[int] = []
    async with async_session_factory() as session:
        async with session.begin():
            conversation_ids = list(
                (
                    await session.scalars(
                        select(Conversation.id).where(Conversation.user_id == user_id)
                    )
                ).all()
            )
            await session.execute(
                delete(ConversationRun).where(ConversationRun.user_id == user_id)
            )
            await session.execute(delete(Conversation).where(Conversation.user_id == user_id))
    for conversation_id in conversation_ids:
        try:
            await redis_client.delete(build_history_key(user_id, conversation_id))
        except RedisError:
            pass


async def main() -> None:
    user_id = "integration-agent-user"
    await cleanup(user_id)
    try:
        async with async_session_factory() as session:
            result = await runtime_harness.run(
                session,
                user_id=user_id,
                message="我是大二学生，想申请国家励志奖学金，需要满足什么条件并准备什么？",
                conversation_id=None,
                grade="大二",
                college="计算机工程学院",
                major="软件工程",
            )
        async with async_session_factory() as trace_session:
            async with trace_session.begin():
                run = await trace_session.get(ConversationRun, result.run_id)
                retrieval_count = len(
                    list(
                        (
                            await trace_session.scalars(
                                select(RetrievalRun).where(RetrievalRun.run_id == result.run_id)
                            )
                        ).all()
                    )
                )
                attempt_count = len(
                    list(
                        (
                            await trace_session.scalars(
                                select(ModelAttempt).where(ModelAttempt.run_id == result.run_id)
                            )
                        ).all()
                    )
                )
        if run is None or run.status != "SUCCEEDED" or retrieval_count != 1 or attempt_count < 1:
            raise AssertionError("ConversationRun Trace 不完整")
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "conversation_id": result.conversation_id,
                    "intent": result.intent,
                    "citation_count": len(result.citations),
                    "conflict_detected": result.conflict_detected,
                    "can_create_task": result.can_create_task,
                    "trace_complete": True,
                    "provider_attempt_count": attempt_count,
                    "answer_preview": result.answer[:160],
                },
                ensure_ascii=False,
            )
        )
    finally:
        await cleanup(user_id)
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
