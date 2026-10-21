"""用真实 MySQL 和缓存验证长期记忆，不调用付费模型。"""
import asyncio
import json
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import delete, func, select

from app.cache.conversation_cache import build_history_key
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory, engine
from app.models import (
    ChatMessage,
    Conversation,
    ConversationFact,
    MemorySnapshot,
)
from app.services.conversation_service import load_conversation_history
from app.services.memory_service import (
    compact_memory,
    confirm_fact,
    load_memory_context,
    revoke_fact,
)
from app.services.persistence_errors import ConversationNotFoundError


class RecordingSummarizer:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.calls: list[list[dict[str, str]]] = []
        self.previous_briefs: list[str | None] = []

    async def summarize(self, *, previous_brief, messages) -> str:
        self.messages = messages
        self.calls.append(messages)
        self.previous_briefs.append(previous_brief)
        prefix = (previous_brief + " | ") if previous_brief else ""
        return prefix + "; ".join(item["content"] for item in messages)


async def main() -> None:
    user_id = "memory-check-" + uuid4().hex[:10]
    conversation_id: int | None = None
    key: str | None = None
    summarizer = RecordingSummarizer()

    async with async_session_factory() as session:
        try:
            async with session.begin():
                conversation = Conversation(user_id=user_id)
                session.add(conversation)
                await session.flush()
                conversation_id = conversation.id
                key = build_history_key(user_id, conversation_id)
                session.add_all(
                    [
                        ChatMessage(
                            conversation_id=conversation_id,
                            role="user" if number % 2 else "assistant",
                            content=f"message-{number}",
                        )
                        for number in range(1, 13)
                    ]
                )

            snapshot = await compact_memory(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                summarizer=summarizer,
            )
            assert snapshot is not None
            v1_version = snapshot.version
            v1_brief = snapshot.brief
            assert [item["content"] for item in summarizer.messages] == [
                "message-1",
                "message-2",
            ]

            async with session.begin():
                session.add_all(
                    [
                        ChatMessage(
                            conversation_id=conversation_id,
                            role="user" if number % 2 else "assistant",
                            content=f"message-{number}",
                        )
                        for number in range(13, 17)
                    ]
                )

            snapshot_v2 = await compact_memory(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                summarizer=summarizer,
            )
            assert snapshot_v2 is not None
            assert snapshot_v2.version == 2
            v2_version = snapshot_v2.version
            assert summarizer.previous_briefs[1] == v1_brief
            assert [item["content"] for item in summarizer.calls[1]] == [
                "message-3",
                "message-4",
                "message-5",
                "message-6",
            ]

            no_snapshot = await compact_memory(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                summarizer=summarizer,
            )
            assert no_snapshot is None

            await confirm_fact(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                key="college",
                value="计算机学院",
            )
            await confirm_fact(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                key="grade",
                value="大二",
            )

            context = await load_memory_context(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            recent = await load_conversation_history(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            assert context.snapshot is not None
            assert context.snapshot.version == 2
            assert len(context.facts) == 2
            assert len(recent) == 10
            assert recent[0]["content"] == "message-7"

            await revoke_fact(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                key="grade",
            )
            after_revoke = await load_memory_context(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            assert [fact.fact_key for fact in after_revoke.facts] == ["college"]
            snapshot_versions = [v1_version, v2_version]
            active_fact_count = len(after_revoke.facts)

            denied = False
            try:
                await load_memory_context(
                    session,
                    user_id="another-user",
                    conversation_id=conversation_id,
                )
            except ConversationNotFoundError:
                denied = True
            assert denied

            print(
                json.dumps(
                    {
                        "snapshot_versions": snapshot_versions,
                        "v1_summarized_messages": len(summarizer.calls[0]),
                        "v2_new_summarized_messages": len(summarizer.calls[1]),
                        "v2_inherited_v1": summarizer.previous_briefs[1] == v1_brief,
                        "immediate_v3_created": no_snapshot is not None,
                        "recent_messages": len(recent),
                        "active_facts_after_revoke": active_fact_count,
                        "wrong_user_denied": denied,
                    }
                )
            )
        finally:
            await session.rollback()
            if conversation_id is not None:
                async with session.begin():
                    await session.execute(
                        delete(Conversation).where(Conversation.id == conversation_id)
                    )
                async with session.begin():
                    snapshot_count = await session.scalar(
                        select(func.count(MemorySnapshot.id)).where(
                            MemorySnapshot.conversation_id == conversation_id
                        )
                    )
                    fact_count = await session.scalar(
                        select(func.count(ConversationFact.id)).where(
                            ConversationFact.conversation_id == conversation_id
                        )
                    )
                assert snapshot_count == 0
                assert fact_count == 0
            if key is not None:
                try:
                    await redis_client.delete(key)
                except RedisError:
                    pass

    await redis_client.aclose()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
