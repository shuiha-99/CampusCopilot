import asyncio
import json

import pytest

from app.core.config import Settings
from app.models import ConversationRun, ModelAttempt
from app.retrieval.hybrid import Evidence, RetrievalResult
from app.runtime import harness as harness_module
from app.runtime.classification import ModelClassification
from app.runtime.harness import RuntimeHarness
from app.services.memory_service import MemoryContext
from app.services.model_errors import (
    ModelAuthenticationError,
    ModelResponseError,
    ModelTimeoutError,
)
from app.services.model_gateway import ModelRouter


class RecordingSession:
    def __init__(self):
        self.added = []

    def begin(self):
        class Transaction:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        return Transaction()

    def add(self, record):
        self.added.append(record)

    async def get(self, model, record_id):
        return next(
            (item for item in self.added if isinstance(item, model) and item.id == record_id),
            None,
        )


class EvidenceRetriever:
    def search(self, query, *, top_k):
        item = Evidence(
            chunk_id="source:chunk", source_key="source", title="测试来源",
            url="https://example.test", text=query + " 申请和准备流程资料",
            scope="测试", collection_status="ACTIVE", validity="UNKNOWN",
            version_label="测试版", published_at="2026-01-01", score=1.0,
            vector_score=1.0, bm25_score=1.0,
        )
        return RetrievalResult(query=query, evidence=[item], conflict_detected=False, warnings=[])


class EmptyRetriever:
    def search(self, query, *, top_k):
        return RetrievalResult(query=query, evidence=[], conflict_detected=False, warnings=[])


class ClassifiedProvider:
    def __init__(self, classification):
        self.classification = classification

    async def generate(self, message, **kwargs):
        if kwargs.get("system_prompt"):
            if isinstance(self.classification, Exception):
                raise self.classification
            return self.classification
        return "申请流程请核对来源[1]。"


def run_case(monkeypatch, message, providers, *, grade=None, college=None, retriever=None):
    session = RecordingSession()
    router = ModelRouter(
        providers, Settings(model_provider="mock"), classification_enabled=True,
    )

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(snapshot=None, facts=[])

    async def save_exchange(*args, **kwargs):
        return 42

    monkeypatch.setattr(harness_module, "model_gateway", router)
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=retriever or EvidenceRetriever()).run(
        session, user_id="user", message=message, conversation_id=None,
        grade=grade, college=college, major=None,
    ))
    run = next(item for item in session.added if isinstance(item, ConversationRun))
    attempts = [item for item in session.added if isinstance(item, ModelAttempt)]
    return result, json.loads(run.blackboard_json), attempts


def test_model_cannot_lower_rule_risk_or_change_known_intent(monkeypatch):
    result, board, attempts = run_case(
        monkeypatch, "帮我创建奖学金准备清单",
        [("primary", ClassifiedProvider('{"intent":"major_transfer","risk_level":"LOW"}'))],
        grade="大二", college="计算机学院",
    )
    assert (result.intent, result.risk_level) == ("scholarship", "HIGH")
    assert board["classification"] == {
        "status": "VALIDATED", "model_intent": "major_transfer", "model_risk_level": "LOW",
    }
    assert [(item.operation, item.status) for item in attempts] == [
        ("classification", "SUCCEEDED"), ("answer", "SUCCEEDED"),
    ]
    assert board["agent_workflow"]["status"] == "SUCCEEDED"
    assert board["agent_workflow"]["artifacts"]["safety_review"]["allow_action"] is True


def test_model_recognized_intent_cannot_grant_action_eligibility(monkeypatch):
    result, board, _ = run_case(
        monkeypatch, "请解释这项学生奖励",
        [("primary", ClassifiedProvider('{"intent":"scholarship","risk_level":"MEDIUM"}'))],
        grade="大二", college="计算机学院",
    )
    assert result.intent == "scholarship"
    assert result.risk_level == "MEDIUM"
    assert result.can_create_task is False
    assert board["classification"]["status"] == "VALIDATED"


@pytest.mark.parametrize("raw", [
    "不是JSON",
    '{"intent":"scholarship","risk_level":"LOW","allow_action":true}',
    '{"intent":"scholarship","risk_level":"SAFE"}',
])
def test_invalid_classification_falls_back_to_program_rules(monkeypatch, raw):
    result, board, attempts = run_case(
        monkeypatch, "奖学金条件是什么",
        [("primary", ClassifiedProvider(raw))],
    )
    assert (result.intent, result.risk_level) == ("scholarship", "LOW")
    assert board["classification"] == {
        "status": "RULE_FALLBACK", "error_code": "ValidationError",
    }
    assert [item.operation for item in attempts] == ["classification", "answer"]


@pytest.mark.parametrize("error", [
    ModelTimeoutError("timeout"),
    ModelAuthenticationError("auth"),
    ModelResponseError("empty"),
])
def test_classification_provider_failure_keeps_safe_rule_fallback(monkeypatch, error):
    primary = ClassifiedProvider(error)
    fallback = ClassifiedProvider('{"intent":"scholarship","risk_level":"LOW"}')
    result, board, attempts = run_case(
        monkeypatch, "帮我创建奖学金准备清单",
        [("primary", primary), ("fallback", fallback)],
        grade="大二", college="计算机学院",
    )
    assert (result.intent, result.risk_level) == ("scholarship", "HIGH")
    assert board["classification"]["status"] == "VALIDATED"
    assert [(item.provider, item.operation, item.status) for item in attempts[:2]] == [
        ("primary", "classification", "FAILED"),
        ("fallback", "classification", "SUCCEEDED"),
    ]


def test_classification_schema_requires_exact_fields():
    with pytest.raises(ValueError):
        ModelClassification.model_validate_json('{"intent":"scholarship"}')


def test_no_evidence_skips_classification_and_answer_model(monkeypatch):
    result, board, attempts = run_case(
        monkeypatch, "2026年奖学金申请截止日是什么时候",
        [("primary", ClassifiedProvider('{"intent":"scholarship","risk_level":"LOW"}'))],
        retriever=EmptyRetriever(),
    )
    assert board["classification"] == {"status": "SKIPPED_NO_EVIDENCE"}
    assert attempts == []
    assert result.citations == []


def test_all_classification_providers_fail_before_answer_uses_rule_fallback(monkeypatch):
    result, board, attempts = run_case(
        monkeypatch, "奖学金申请条件是什么",
        [("primary", ClassifiedProvider(ModelTimeoutError("classification timeout")))],
    )
    assert (result.intent, result.risk_level) == ("scholarship", "LOW")
    assert board["classification"] == {
        "status": "RULE_FALLBACK", "error_code": "ModelTimeoutError",
    }
    assert [(item.operation, item.status) for item in attempts] == [
        ("classification", "FAILED"), ("answer", "SUCCEEDED"),
    ]
