import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api import agent as agent_module
from app.models import ConversationRun, ModelAttempt, RetrievalRun
from app.retrieval import EmbeddingServiceError
from app.runtime.harness import RuntimeHarness
from main import app


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def begin(self) -> _Transaction:
        return _Transaction()

    def add(self, record: object) -> None:
        self.added.append(record)

    async def get(self, model, record_id: str):
        return next(
            (
                record
                for record in self.added
                if isinstance(record, model) and getattr(record, "id", None) == record_id
            ),
            None,
        )


class FailingRetriever:
    def search(self, *args, **kwargs):
        raise EmbeddingServiceError("测试Embedding连接失败")


def test_retrieval_failure_leaves_failed_conversation_run() -> None:
    session = RecordingSession()
    harness = RuntimeHarness(retriever=FailingRetriever())

    with pytest.raises(EmbeddingServiceError):
        asyncio.run(
            harness.run(
                session,
                user_id="retrieval-failure-user",
                message="国家励志奖学金需要什么条件？",
                conversation_id=None,
                grade="大二",
                college="计算机工程学院",
                major="软件工程",
            )
        )

    run = next(record for record in session.added if isinstance(record, ConversationRun))
    assert run.status == "FAILED"
    assert run.error_code == "EmbeddingServiceError"
    assert run.finished_at is not None
    assert not any(isinstance(record, RetrievalRun) for record in session.added)
    assert not any(isinstance(record, ModelAttempt) for record in session.added)


def test_agent_returns_503_for_retrieval_failure(monkeypatch) -> None:
    async def fail_run(*args, **kwargs):
        raise EmbeddingServiceError("不应返回给客户端的内部错误")

    monkeypatch.setattr(agent_module.runtime_harness, "run", fail_run)

    response = TestClient(app).post(
        "/agent/query",
        json={
            "user_id": "retrieval-failure-user",
            "message": "国家励志奖学金需要什么条件？",
            "grade": "大二",
            "college": "计算机工程学院",
            "major": "软件工程",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "政策检索服务暂时不可用"}
