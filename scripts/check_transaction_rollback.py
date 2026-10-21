import asyncio
from uuid import uuid4

from sqlalchemy import func, select
from app.services.persistence_errors import PersistenceError

from app.db.session import async_session_factory, engine
from app.models import ChatMessage, Conversation
from app.services.conversation_service import create_exchange


async def main() -> None:
    probe_id = uuid4().hex
    probe_user = f"rollback-{probe_id}"
    probe_question = f"rollback-question-{probe_id}"

    try:
        async with async_session_factory() as session:
            await create_exchange(
                session,
                user_id=probe_user,
                question=probe_question,
                # 故意违反 content 非空约束，制造提交失败。
                answer=None,  # type: ignore[arg-type]
            )
    except PersistenceError:
        print("EXPECTED_PERSISTENCE_ERROR")
    else:
        raise AssertionError("预期的持久化错误没有发生")

    async with async_session_factory() as session:
        conversation_count = await session.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.user_id == probe_user
            )
        )
        message_count = await session.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.content == probe_question
            )
        )

    print(f"remaining_conversations={conversation_count}")
    print(f"remaining_messages={message_count}")

    assert conversation_count == 0
    assert message_count == 0

    print("ROLLBACK_OK")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
