import asyncio

from app.services import conversation_service as service
from types import SimpleNamespace

from redis.exceptions import RedisError


def test_load_history_returns_redis_hit_without_database(
    monkeypatch,
) -> None:
    expected_history = [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    async def fake_load_cached_history(
        *,
        user_id: str,
        conversation_id: int,
    ) -> list[dict[str, str]]:
        assert user_id == "test-user"
        assert conversation_id == 7
        return expected_history

    class DatabaseMustNotBeUsed:
        def begin(self):
            raise AssertionError("缓存命中时不应该访问 MySQL")

    monkeypatch.setattr(
        service,
        "load_cached_history",
        fake_load_cached_history,
    )

    history = asyncio.run(
        service.load_conversation_history(
            DatabaseMustNotBeUsed(),
            user_id="test-user",
            conversation_id=7,
        )
    )

    assert history == expected_history


def test_load_history_falls_back_to_database_when_redis_fails(
    monkeypatch,
) -> None:
    cache_operations: list[str] = []

    async def fail_to_load_cache(
        *,
        user_id: str,
        conversation_id: int,
    ) -> None:
        cache_operations.append("read")
        raise RedisError("测试 Redis 读取失败")

    async def fail_to_fill_cache(
        *,
        user_id: str,
        conversation_id: int,
        history: list[dict[str, str]],
    ) -> None:
        cache_operations.append("write")
        raise RedisError("测试 Redis 写入失败")

    class FakeTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeScalarResult:
        def all(self):
            # 模拟数据库按照消息 ID 倒序返回。
            return [
                SimpleNamespace(
                    role="assistant",
                    content="上一轮回答",
                ),
                SimpleNamespace(
                    role="user",
                    content="上一轮问题",
                ),
            ]

    class FakeDatabaseSession:
        def __init__(self) -> None:
            self.was_used = False

        def begin(self):
            self.was_used = True
            return FakeTransaction()

        async def scalar(self, statement):
            return 7

        async def scalars(self, statement):
            return FakeScalarResult()

    monkeypatch.setattr(
        service,
        "load_cached_history",
        fail_to_load_cache,
    )
    monkeypatch.setattr(
        service,
        "replace_cached_history",
        fail_to_fill_cache,
    )

    session = FakeDatabaseSession()

    history = asyncio.run(
        service.load_conversation_history(
            session,
            user_id="test-user",
            conversation_id=7,
        )
    )

    assert session.was_used is True
    assert cache_operations == ["read", "write"]
    assert history == [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]
