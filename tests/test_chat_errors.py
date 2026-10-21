import pytest
from fastapi.testclient import TestClient

from app.api import chat as chat_module
from app.services.model_errors import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelGatewayError,
    ModelResponseError,
    ModelTimeoutError,
)
from main import app
from app.services.persistence_errors import (
    ConversationNotFoundError,
    PersistenceError,
)
from app.services.memory_service import MemoryContext


@pytest.fixture(autouse=True)
def use_empty_memory_context(monkeypatch) -> None:
    async def fake_load_memory(*args, **kwargs) -> MemoryContext:
        return MemoryContext(snapshot=None, facts=[])

    monkeypatch.setattr(chat_module, "load_memory_context", fake_load_memory)


class FailingGateway:
    def __init__(self, error: ModelGatewayError) -> None:
        self._error = error

    async def generate(
        self,
        message: str,
        *,
        history: list[dict[str, str]] | None = None,
        context: str | None = None,
    ) -> str:
        raise self._error


class SuccessfulGateway:
    async def generate(
        self,
        message: str,
        *,
        history: list[dict[str, str]] | None = None,
        context: str | None = None,
    ) -> str:
        return "测试模型回答"


@pytest.mark.parametrize(
    ("model_error", "expected_status", "expected_detail"),
    [
        (
            ModelTimeoutError("测试超时"),
            504,
            "模型响应超时，请稍后重试",
        ),
        (
            ModelConnectionError("测试连接失败"),
            503,
            "暂时无法连接模型服务，请稍后重试",
        ),
        (
            ModelAuthenticationError("测试认证失败"),
            502,
            "模型服务暂时不可用",
        ),
        (
            ModelResponseError("测试无效响应"),
            502,
            "模型服务返回了无效结果",
        ),
    ],
)
def test_chat_converts_model_error_to_http_response(
    monkeypatch,
    model_error: ModelGatewayError,
    expected_status: int,
    expected_detail: str,
) -> None:
    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        FailingGateway(model_error),
    )

    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "user_id": "test-user",
            "message": "奖学金怎么申请？",
        },
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}


def test_chat_returns_503_when_persistence_fails(
    monkeypatch,
) -> None:
    async def fail_create_exchange(*args, **kwargs) -> int:
        raise PersistenceError("包含内部数据库信息的测试错误")

    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        SuccessfulGateway(),
    )
    monkeypatch.setattr(
        chat_module,
        "create_exchange",
        fail_create_exchange,
    )

    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "user_id": "test-user",
            "message": "奖学金怎么申请？",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "暂时无法保存对话，请稍后重试"}


def test_chat_returns_saved_conversation(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_load_history(*args, **kwargs) -> list[dict[str, str]]:
        return []

    async def fake_create_exchange(
        session,
        *,
        user_id: str,
        question: str,
        answer: str,
        conversation_id: int | None = None,
    ) -> int:
        captured["user_id"] = user_id
        captured["question"] = question
        captured["answer"] = answer
        captured["conversation_id"] = conversation_id
        return 42

    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        SuccessfulGateway(),
    )
    monkeypatch.setattr(
        chat_module,
        "load_conversation_history",
        fake_load_history,
    )
    monkeypatch.setattr(
        chat_module,
        "create_exchange",
        fake_create_exchange,
    )

    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "user_id": "test-user",
            "message": "奖学金怎么申请？",
            "conversation_id": 7,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": 42,
        "answer": "测试模型回答",
        "evidence_available": False,
    }
    assert captured == {
        "user_id": "test-user",
        "question": "奖学金怎么申请？",
        "answer": "测试模型回答",
        "conversation_id": 7,
    }


def test_chat_returns_404_when_conversation_is_not_accessible(
    monkeypatch,
) -> None:
    async def fail_load_history(*args, **kwargs) -> list[dict[str, str]]:
        raise ConversationNotFoundError("内部错误详情")

    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        SuccessfulGateway(),
    )
    monkeypatch.setattr(
        chat_module,
        "load_conversation_history",
        fail_load_history,
    )

    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "user_id": "test-user",
            "message": "继续刚才的问题",
            "conversation_id": 7,
        },
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "会话不存在或无权访问",
    }


def test_chat_passes_history_to_model(
    monkeypatch,
) -> None:
    expected_history = [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]
    captured: dict[str, object] = {}

    async def fake_load_history(*args, **kwargs) -> list[dict[str, str]]:
        return expected_history

    class CapturingGateway:
        async def generate(
            self,
            message: str,
            *,
            history: list[dict[str, str]] | None = None,
            context: str | None = None,
        ) -> str:
            captured["message"] = message
            captured["history"] = history
            captured["context"] = context
            return "包含历史的测试回答"

    async def fake_create_exchange(*args, **kwargs) -> int:
        return 7

    monkeypatch.setattr(
        chat_module,
        "load_conversation_history",
        fake_load_history,
    )
    monkeypatch.setattr(
        chat_module,
        "model_gateway",
        CapturingGateway(),
    )
    monkeypatch.setattr(
        chat_module,
        "create_exchange",
        fake_create_exchange,
    )

    client = TestClient(app)
    response = client.post(
        "/chat",
        json={
            "user_id": "test-user",
            "message": "这一轮问题",
            "conversation_id": 7,
        },
    )

    assert response.status_code == 200
    assert captured == {
        "message": "这一轮问题",
        "history": expected_history,
        "context": None,
    }


def test_chat_passes_long_term_memory_to_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_load_history(*args, **kwargs) -> list[dict[str, str]]:
        return []

    async def fake_load_memory(*args, **kwargs) -> MemoryContext:
        from types import SimpleNamespace

        return MemoryContext(
            snapshot=SimpleNamespace(brief="用户正在准备奖学金申请。"),
            facts=[SimpleNamespace(fact_key="grade", fact_value="大二")],
        )

    class CapturingGateway:
        async def generate(
            self,
            message: str,
            *,
            history: list[dict[str, str]] | None = None,
            context: str | None = None,
        ) -> str:
            captured["context"] = context
            return "测试回答"

    async def fake_create_exchange(*args, **kwargs) -> int:
        return 7

    monkeypatch.setattr(chat_module, "load_conversation_history", fake_load_history)
    monkeypatch.setattr(chat_module, "load_memory_context", fake_load_memory)
    monkeypatch.setattr(chat_module, "model_gateway", CapturingGateway())
    monkeypatch.setattr(chat_module, "create_exchange", fake_create_exchange)

    response = TestClient(app).post(
        "/chat",
        json={
            "user_id": "test-user",
            "message": "我需要准备什么？",
            "conversation_id": 7,
        },
    )

    assert response.status_code == 200
    assert captured["context"] == (
        "较早对话摘要：\n用户正在准备奖学金申请。\n\n"
        "用户明确确认的事实：\n- grade: 大二"
    )
