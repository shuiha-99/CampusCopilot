import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.api import chat as chat_module
from app.services.memory_service import MemoryContext
from app.services.model_errors import ModelConnectionError, ModelTimeoutError
from app.services.persistence_errors import PersistenceError
from main import app


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event, data))
    return events


@pytest.fixture(autouse=True)
def isolate_stream_dependencies(monkeypatch) -> None:
    async def fake_load_history(*args, **kwargs) -> list[dict[str, str]]:
        return []

    async def fake_load_memory(*args, **kwargs) -> MemoryContext:
        return MemoryContext(snapshot=None, facts=[])

    @asynccontextmanager
    async def fake_session_factory():
        yield object()

    monkeypatch.setattr(chat_module, "load_conversation_history", fake_load_history)
    monkeypatch.setattr(chat_module, "load_memory_context", fake_load_memory)
    monkeypatch.setattr(chat_module, "async_session_factory", fake_session_factory)


def test_stream_chat_saves_complete_answer_and_finishes_with_metadata(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class SuccessfulStreamingGateway:
        async def stream_generate(self, *args, **kwargs):
            yield "测试"
            yield "回答"

    async def fake_create_exchange(
        session,
        *,
        user_id: str,
        question: str,
        answer: str,
        conversation_id: int | None = None,
    ) -> int:
        captured.update(
            user_id=user_id,
            question=question,
            answer=answer,
            conversation_id=conversation_id,
        )
        return 42

    monkeypatch.setattr(chat_module, "model_gateway", SuccessfulStreamingGateway())
    monkeypatch.setattr(chat_module, "create_exchange", fake_create_exchange)

    response = TestClient(app).post(
        "/chat/stream",
        json={"user_id": "test-user", "message": "奖学金怎么申请？"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert parse_sse(response.text) == [
        ("token", {"delta": "测试"}),
        ("token", {"delta": "回答"}),
        ("done", {"conversation_id": 42, "evidence_available": False}),
    ]
    assert captured == {
        "user_id": "test-user",
        "question": "奖学金怎么申请？",
        "answer": "测试回答",
        "conversation_id": None,
    }


def test_stream_chat_returns_http_error_before_first_token(monkeypatch) -> None:
    class FailingBeforeFirstTokenGateway:
        async def stream_generate(self, *args, **kwargs):
            if False:
                yield "never"
            raise ModelTimeoutError("测试首片段前超时")

    async def should_not_save(*args, **kwargs) -> int:
        raise AssertionError("首片段前失败时不应保存")

    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        FailingBeforeFirstTokenGateway(),
    )
    monkeypatch.setattr(chat_module, "create_exchange", should_not_save)

    response = TestClient(app).post(
        "/chat/stream",
        json={"user_id": "test-user", "message": "奖学金怎么申请？"},
    )

    assert response.status_code == 504
    assert response.json() == {"detail": "模型响应超时，请稍后重试"}


def test_stream_chat_emits_error_after_first_token_without_saving(
    monkeypatch,
) -> None:
    class FailingAfterFirstTokenGateway:
        async def stream_generate(self, *args, **kwargs):
            yield "部分回答"
            raise ModelConnectionError("测试输出后断流")

    async def should_not_save(*args, **kwargs) -> int:
        raise AssertionError("不完整回答不应保存")

    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        FailingAfterFirstTokenGateway(),
    )
    monkeypatch.setattr(chat_module, "create_exchange", should_not_save)

    response = TestClient(app).post(
        "/chat/stream",
        json={"user_id": "test-user", "message": "奖学金怎么申请？"},
    )

    assert response.status_code == 200
    assert parse_sse(response.text) == [
        ("token", {"delta": "部分回答"}),
        (
            "error",
            {
                "code": "model_connection_error",
                "message": "模型连接中断，请稍后重试",
                "retryable": True,
            },
        ),
    ]


def test_stream_chat_reports_save_failure_after_answer(monkeypatch) -> None:
    class SuccessfulStreamingGateway:
        async def stream_generate(self, *args, **kwargs):
            yield "完整回答"

    async def fail_to_save(*args, **kwargs) -> int:
        raise PersistenceError("测试数据库不可用")

    monkeypatch.setattr(chat_module, "model_gateway", SuccessfulStreamingGateway())
    monkeypatch.setattr(chat_module, "create_exchange", fail_to_save)

    response = TestClient(app).post(
        "/chat/stream",
        json={"user_id": "test-user", "message": "奖学金怎么申请？"},
    )

    assert response.status_code == 200
    assert parse_sse(response.text) == [
        ("token", {"delta": "完整回答"}),
        (
            "error",
            {
                "code": "persistence_unavailable",
                "message": "回答已生成，但暂时无法保存，请稍后重试",
                "retryable": True,
            },
        ),
    ]
