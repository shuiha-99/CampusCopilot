import logging
from json import JSONDecodeError

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.conversation_cache import (
    append_cached_exchange,
    load_cached_history,
    replace_cached_history,
)
from app.models import ChatMessage, Conversation
from app.services.persistence_errors import (
    ConversationNotFoundError,
    PersistenceError,
)

HISTORY_MESSAGE_LIMIT = 10

logger = logging.getLogger(__name__)


async def load_conversation_history(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int | None,
) -> list[dict[str, str]]:
    if conversation_id is None:
        return []

    try:
        cached_history = await load_cached_history(
            user_id=user_id,
            conversation_id=conversation_id,
        )
    except (RedisError, JSONDecodeError):
        logger.warning(
            "读取 Redis 会话缓存失败，回退 MySQL",
            exc_info=True,
        )
        cached_history = None

    if cached_history is not None:
        return cached_history

    try:
        async with session.begin():
            conversation_statement = select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
            existing_conversation_id = await session.scalar(conversation_statement)

            if existing_conversation_id is None:
                raise ConversationNotFoundError("会话不存在或不属于当前用户")

            message_statement = (
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_MESSAGE_LIMIT)
            )
            result = await session.scalars(message_statement)
            messages = list(result.all())
    except ConversationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("读取对话历史失败") from exc

    messages.reverse()
    history = [
        {
            "role": message.role,
            "content": message.content,
        }
        for message in messages
    ]

    try:
        await replace_cached_history(
            user_id=user_id,
            conversation_id=conversation_id,
            history=history,
        )
    except RedisError:
        logger.warning(
            "写入 Redis 会话缓存失败，本次继续使用 MySQL 结果",
            exc_info=True,
        )

    return history


async def create_exchange(
    session: AsyncSession,
    *,
    user_id: str,
    question: str,
    answer: str,
    conversation_id: int | None = None,
) -> int:
    try:
        async with session.begin():
            if conversation_id is None:
                conversation = Conversation(
                    user_id=user_id,
                )
                session.add(conversation)

                # 必须先 flush，才能取得新会话的自增 ID。
                await session.flush()
            else:
                statement = select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
                conversation = await session.scalar(statement)

                if conversation is None:
                    raise ConversationNotFoundError("会话不存在或不属于当前用户")

            session.add_all(
                [
                    ChatMessage(
                        conversation_id=conversation.id,
                        role="user",
                        content=question,
                    ),
                    ChatMessage(
                        conversation_id=conversation.id,
                        role="assistant",
                        content=answer,
                    ),
                ]
            )

    except ConversationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("保存对话失败") from exc

    try:
        await append_cached_exchange(
            user_id=user_id,
            conversation_id=conversation.id,
            question=question,
            answer=answer,
        )
    except RedisError:
        logger.warning(
            "追加 Redis 会话缓存失败，MySQL 数据已经保存",
            exc_info=True,
        )

    return conversation.id
