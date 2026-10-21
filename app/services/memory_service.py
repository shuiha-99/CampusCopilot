from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage, Conversation, ConversationFact, MemorySnapshot
from app.services.persistence_errors import ConversationNotFoundError, PersistenceError

RECENT_MESSAGE_LIMIT = 10


class MemorySummarizer(Protocol):
    async def summarize(
        self,
        *,
        previous_brief: str | None,
        messages: list[dict[str, str]],
    ) -> str: ...


@dataclass(frozen=True)
class MemoryContext:
    snapshot: MemorySnapshot | None
    facts: list[ConversationFact]

    def as_model_context(self) -> str | None:
        sections: list[str] = []
        if self.snapshot is not None:
            sections.append("较早对话摘要：\n" + self.snapshot.brief)
        if self.facts:
            facts = "\n".join(f"- {fact.fact_key}: {fact.fact_value}" for fact in self.facts)
            sections.append("用户明确确认的事实：\n" + facts)
        return "\n\n".join(sections) or None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _require_conversation(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int,
) -> None:
    statement = select(Conversation.id).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    )
    if await session.scalar(statement) is None:
        raise ConversationNotFoundError("会话不存在或不属于当前用户")


async def load_memory_context(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int | None,
) -> MemoryContext:
    if conversation_id is None:
        return MemoryContext(snapshot=None, facts=[])

    try:
        async with session.begin():
            await _require_conversation(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            snapshot = await session.scalar(
                select(MemorySnapshot)
                .where(MemorySnapshot.conversation_id == conversation_id)
                .order_by(MemorySnapshot.version.desc())
                .limit(1)
            )
            result = await session.scalars(
                select(ConversationFact)
                .where(
                    ConversationFact.conversation_id == conversation_id,
                    ConversationFact.is_active.is_(True),
                )
                .order_by(ConversationFact.fact_key)
            )
            facts = list(result.all())
    except ConversationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("读取长期记忆失败") from exc

    return MemoryContext(snapshot=snapshot, facts=facts)


async def confirm_fact(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int,
    key: str,
    value: str,
) -> ConversationFact:
    try:
        async with session.begin():
            await _require_conversation(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            fact = await session.scalar(
                select(ConversationFact).where(
                    ConversationFact.conversation_id == conversation_id,
                    ConversationFact.fact_key == key,
                )
            )
            if fact is None:
                fact = ConversationFact(
                    conversation_id=conversation_id,
                    fact_key=key,
                    fact_value=value,
                    is_active=True,
                )
                session.add(fact)
            else:
                fact.fact_value = value
                fact.is_active = True
                fact.confirmed_at = _now()
                fact.revoked_at = None
            await session.flush()
    except ConversationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("保存确认事实失败") from exc
    return fact


async def revoke_fact(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int,
    key: str,
) -> ConversationFact | None:
    try:
        async with session.begin():
            await _require_conversation(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            fact = await session.scalar(
                select(ConversationFact).where(
                    ConversationFact.conversation_id == conversation_id,
                    ConversationFact.fact_key == key,
                    ConversationFact.is_active.is_(True),
                )
            )
            if fact is not None:
                fact.is_active = False
                fact.revoked_at = _now()
                await session.flush()
    except ConversationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("撤销确认事实失败") from exc
    return fact


async def compact_memory(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int,
    summarizer: MemorySummarizer,
) -> MemorySnapshot | None:
    """只摘要已经滑出近期十条窗口且尚未被快照覆盖的消息。"""
    try:
        async with session.begin():
            await _require_conversation(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            recent_result = await session.scalars(
                select(ChatMessage.id)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id.desc())
                .limit(RECENT_MESSAGE_LIMIT)
            )
            recent_ids = list(recent_result.all())
            if len(recent_ids) < RECENT_MESSAGE_LIMIT:
                return None

            latest = await session.scalar(
                select(MemorySnapshot)
                .where(MemorySnapshot.conversation_id == conversation_id)
                .order_by(MemorySnapshot.version.desc())
                .limit(1)
            )
            covered_id = latest.covered_message_id if latest else 0
            oldest_recent_id = min(recent_ids)
            message_result = await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.id > covered_id,
                    ChatMessage.id < oldest_recent_id,
                )
                .order_by(ChatMessage.id)
            )
            messages = list(message_result.all())
            previous_brief = latest.brief if latest else None
            next_version = latest.version + 1 if latest else 1
    except ConversationNotFoundError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("读取待摘要消息失败") from exc

    if not messages:
        return None

    model_messages = [
        {"role": message.role, "content": message.content}
        for message in messages
    ]
    brief = (await summarizer.summarize(
        previous_brief=previous_brief,
        messages=model_messages,
    )).strip()
    if not brief:
        raise PersistenceError("模型返回了空的记忆摘要")

    snapshot = MemorySnapshot(
        conversation_id=conversation_id,
        version=next_version,
        brief=brief,
        covered_message_id=messages[-1].id,
        input_chars=len(previous_brief or "")
        + sum(len(message["content"]) for message in model_messages),
        output_chars=len(brief),
    )
    try:
        async with session.begin():
            session.add(snapshot)
            await session.flush()
    except SQLAlchemyError as exc:
        raise PersistenceError("保存记忆摘要失败") from exc
    return snapshot
