import json

from app.cache.redis_client import redis_client

HISTORY_CACHE_TTL_SECONDS = 30 * 60
HISTORY_CACHE_MESSAGE_LIMIT = 10


def build_history_key(user_id: str, conversation_id: int) -> str:
    return f"campus_copilot:conversation:{user_id}:{conversation_id}:history"


async def load_cached_history(
    *,
    user_id: str,
    conversation_id: int,
) -> list[dict[str, str]] | None:
    key = build_history_key(user_id, conversation_id)
    cached_messages = await redis_client.lrange(key, 0, -1)

    if not cached_messages:
        return None

    return [json.loads(message) for message in cached_messages]


async def replace_cached_history(
    *,
    user_id: str,
    conversation_id: int,
    history: list[dict[str, str]],
) -> None:
    key = build_history_key(user_id, conversation_id)
    recent_history = history[-HISTORY_CACHE_MESSAGE_LIMIT:]

    async with redis_client.pipeline(transaction=True) as pipeline:
        pipeline.delete(key)

        for message in recent_history:
            pipeline.rpush(
                key,
                json.dumps(message, ensure_ascii=False),
            )

        pipeline.expire(key, HISTORY_CACHE_TTL_SECONDS)
        await pipeline.execute()


async def append_cached_exchange(
    *,
    user_id: str,
    conversation_id: int,
    question: str,
    answer: str,
) -> None:
    key = build_history_key(user_id, conversation_id)

    serialized_messages = [
        json.dumps(
            {"role": "user", "content": question},
            ensure_ascii=False,
        ),
        json.dumps(
            {"role": "assistant", "content": answer},
            ensure_ascii=False,
        ),
    ]

    async with redis_client.pipeline(transaction=True) as pipeline:
        pipeline.rpush(key, *serialized_messages)
        pipeline.ltrim(key, -HISTORY_CACHE_MESSAGE_LIMIT, -1)
        pipeline.expire(key, HISTORY_CACHE_TTL_SECONDS)
        await pipeline.execute()
