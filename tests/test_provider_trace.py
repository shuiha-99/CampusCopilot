import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models import ConversationRun, ModelAttempt, RetrievalRun
from app.retrieval.hybrid import Evidence, RetrievalResult
from app.runtime import harness as harness_module
from app.runtime.harness import RuntimeHarness
from app.services.memory_service import MemoryContext
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


class FailingProvider:
    async def generate(self, *args, **kwargs):
        from app.services.model_errors import ModelConnectionError

        raise ModelConnectionError("primary unavailable")


class WorkingProvider:
    async def generate(self, *args, **kwargs):
        self.last_context = kwargs.get("context")
        return "带来源的测试回答[1]"


class EmptyRetriever:
    def search(self, query, *, top_k):
        return RetrievalResult(query=query, evidence=[], conflict_detected=False, warnings=[])


class EvidenceRetriever:
    def search(self, query, *, top_k):
        item = Evidence(
            chunk_id="source:chunk", source_key="source", title="测试来源", url="https://example.test",
            text="奖学金申请测试资料", scope="测试", collection_status="ACTIVE", validity="UNKNOWN",
            version_label="测试版", published_at="2026-01-01", score=1.0,
            vector_score=1.0, bm25_score=1.0,
        )
        return RetrievalResult(query=query, evidence=[item], conflict_detected=False, warnings=[])


class OldEvidenceRetriever(EvidenceRetriever):
    def search(self, query, *, top_k):
        result = super().search(query, top_k=top_k)
        return replace(result, evidence=[replace(result.evidence[0], published_at="2025-03-26")])


def test_agent_run_persists_failed_and_successful_provider_attempts(monkeypatch) -> None:
    session = RecordingSession()
    working = WorkingProvider()
    router = ModelRouter(
        [("deepseek", FailingProvider()), ("ollama", working)],
        Settings(model_provider="mock"),
    )

    async def empty_history(*args, **kwargs):
        return [{"role": "user", "content": "先前问题"}]

    async def empty_memory(*args, **kwargs):
        return MemoryContext(
            snapshot=SimpleNamespace(
                id=17, version=2, covered_message_id=6, brief="用户正在准备奖学金申请。"
            ),
            facts=[SimpleNamespace(fact_key="college", fact_value="计算机学院")],
        )

    async def save_exchange(*args, **kwargs):
        return 42

    monkeypatch.setattr(harness_module, "model_gateway", router)
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=EvidenceRetriever()).run(
        session,
        user_id="trace-user",
        message="奖学金条件是什么？",
        conversation_id=None,
        grade="大二",
        college="计算机学院",
        major=None,
    ))

    attempts = [item for item in session.added if isinstance(item, ModelAttempt)]
    assert [(item.provider, item.operation, item.status, item.error_code) for item in attempts] == [
        ("deepseek", "answer", "FAILED", "ModelConnectionError"),
        ("ollama", "answer", "SUCCEEDED", None),
    ]
    assert all(item.run_id == result.run_id for item in attempts)
    run = next(item for item in session.added if isinstance(item, ConversationRun))
    assert run.status == "SUCCEEDED"
    assert "用户正在准备奖学金申请。" in working.last_context
    assert json.loads(run.blackboard_json)["memory"] == {
        "snapshot_id": 17,
        "snapshot_version": 2,
        "covered_message_id": 6,
        "active_fact_keys": ["college"],
        "recent_message_count": 1,
        "used_in_model_context": True,
    }


def test_no_evidence_refuses_without_calling_any_provider(monkeypatch) -> None:
    session = RecordingSession()

    class ForbiddenProvider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("model must not be called without evidence")

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(
            snapshot=SimpleNamespace(id=19, version=3, covered_message_id=8, brief="旧消息摘要"),
            facts=[],
        )

    async def save_exchange(*args, **kwargs):
        return 43

    monkeypatch.setattr(harness_module, "model_gateway", ForbiddenProvider())
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=EmptyRetriever()).run(
        session, user_id="no-evidence-user", message="没有资料的问题", conversation_id=None,
        grade=None, college=None, major=None,
    ))

    assert "没有找到足够证据" in result.answer
    assert "[1]" not in result.answer
    assert result.citations == []
    assert not any(isinstance(item, ModelAttempt) for item in session.added)
    run = next(item for item in session.added if isinstance(item, ConversationRun))
    assert json.loads(run.blackboard_json)["memory"]["snapshot_version"] == 3
    assert json.loads(run.blackboard_json)["memory"]["used_in_model_context"] is False


def test_memory_trace_does_not_claim_model_use_when_router_has_no_attempt(monkeypatch) -> None:
    from app.services.model_errors import ModelConnectionError

    session = RecordingSession()

    async def empty_history(*args, **kwargs):
        return []

    async def loaded_memory(*args, **kwargs):
        return MemoryContext(
            snapshot=SimpleNamespace(id=23, version=4, covered_message_id=12, brief="旧消息摘要"),
            facts=[],
        )

    monkeypatch.setattr(
        harness_module, "model_gateway", ModelRouter([], Settings(model_provider="mock"))
    )
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", loaded_memory)

    with pytest.raises(ModelConnectionError):
        asyncio.run(RuntimeHarness(retriever=EvidenceRetriever()).run(
            session, user_id="no-provider", message="奖学金申请条件是什么？",
            conversation_id=None, grade="大二", college="计算机学院", major=None,
        ))
    run = next(item for item in session.added if isinstance(item, ConversationRun))
    assert run.status == "FAILED"
    assert json.loads(run.blackboard_json)["memory"]["snapshot_version"] == 4
    assert json.loads(run.blackboard_json)["memory"]["used_in_model_context"] is False
    assert not any(isinstance(item, ModelAttempt) for item in session.added)


def test_wrong_year_sources_are_traceable_but_cannot_support_answer(monkeypatch) -> None:
    session = RecordingSession()

    class ForbiddenProvider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("old evidence must not reach the model")

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(snapshot=None, facts=[])

    async def save_exchange(*args, **kwargs):
        return 44

    monkeypatch.setattr(harness_module, "model_gateway", ForbiddenProvider())
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=OldEvidenceRetriever()).run(
        session, user_id="year-user", message="2026年国家励志奖学金申请截止日期是哪天",
        conversation_id=None, grade="大二", college="计算机学院", major=None,
    ))

    assert "不能可靠回答" in result.answer
    assert "2025-03-26" in result.answer and "仅供历史对照" in result.answer
    assert result.citations == []
    assert result.can_create_task is False
    assert not any(isinstance(item, ModelAttempt) for item in session.added)
    trace = next(item for item in session.added if isinstance(item, RetrievalRun))
    assert json.loads(trace.result_json)[0]["published_at"] == "2025-03-26"


def test_explicit_year_filter_keeps_only_matching_years() -> None:
    item = EvidenceRetriever().search("query", top_k=5).evidence[0]
    old_item = replace(item, chunk_id="old", published_at="2024-05-10")
    new_item = replace(item, chunk_id="new", published_at="2025-03-26")
    assert RuntimeHarness._evidence_for_requested_year("2025年奖学金", [old_item, new_item]) == [new_item]
    assert RuntimeHarness._evidence_for_requested_year("2024年和2025年奖学金", [old_item, new_item]) == [old_item, new_item]
    assert RuntimeHarness._evidence_for_requested_year("2024年和2026年奖学金", [old_item, new_item]) == []


def test_food_price_topic_mismatch_refuses_even_with_raw_candidates(monkeypatch) -> None:
    session = RecordingSession()

    class ForbiddenProvider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("unrelated policy chunks must not reach the model")

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(snapshot=None, facts=[])

    async def save_exchange(*args, **kwargs):
        return 45

    monkeypatch.setattr(harness_module, "model_gateway", ForbiddenProvider())
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=EvidenceRetriever()).run(
        session, user_id="food-user", message="学校食堂一份鸡腿现在多少钱",
        conversation_id=None, grade=None, college=None, major=None,
    ))

    assert "不能可靠回答" in result.answer
    assert "https://example.test" not in result.answer
    assert result.citations == []
    assert not any(isinstance(item, ModelAttempt) for item in session.added)
    trace = next(item for item in session.added if isinstance(item, RetrievalRun))
    assert len(json.loads(trace.result_json)) == 1


def test_invalid_model_citation_is_replaced_by_safe_answer(monkeypatch) -> None:
    session = RecordingSession()

    class BadCitationProvider:
        async def generate(self, *args, **kwargs):
            return "未经来源支持的金额是9999元[9]"

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(snapshot=None, facts=[])

    async def save_exchange(*args, **kwargs):
        return 46

    monkeypatch.setattr(harness_module, "model_gateway", BadCitationProvider())
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=EvidenceRetriever()).run(
        session, user_id="citation-user", message="奖学金申请测试资料是什么",
        conversation_id=None, grade="大二", college="计算机学院", major=None,
    ))

    assert "引用无法核实" in result.answer
    assert "9999" not in result.answer
    assert result.citations == []
    assert result.can_create_task is False
    attempts = [item for item in session.added if isinstance(item, ModelAttempt)]
    assert len(attempts) == 1 and attempts[0].status == "SUCCEEDED"


def test_valid_citation_is_renumbered_to_match_returned_sources(monkeypatch) -> None:
    session = RecordingSession()

    class TwoEvidenceRetriever(EvidenceRetriever):
        def search(self, query, *, top_k):
            result = super().search(query, top_k=top_k)
            second = replace(result.evidence[0], chunk_id="source:second", title="第二份资料")
            return replace(result, evidence=[result.evidence[0], second])

    class SecondCitationProvider:
        async def generate(self, *args, **kwargs):
            return "资料说明奖学金申请条件[2]。"

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(snapshot=None, facts=[])

    async def save_exchange(*args, **kwargs):
        return 47

    monkeypatch.setattr(harness_module, "model_gateway", SecondCitationProvider())
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=TwoEvidenceRetriever()).run(
        session, user_id="citation-number-user", message="奖学金申请测试资料是什么",
        conversation_id=None, grade="大二", college="计算机学院", major=None,
    ))

    assert "[1]" in result.answer and "[2]" not in result.answer
    assert [item.chunk_id for item in result.citations] == ["source:second"]
    trace = next(item for item in session.added if isinstance(item, RetrievalRun))
    assert len(json.loads(trace.result_json)) == 2


def test_unsupported_grade_claim_is_not_sent_as_grounded_answer(monkeypatch) -> None:
    session = RecordingSession()

    class MisattributedProvider:
        async def generate(self, *args, **kwargs):
            return "根据测试来源[1]，国家励志奖学金面向二年级以上学生。"

    async def empty_history(*args, **kwargs):
        return []

    async def empty_memory(*args, **kwargs):
        return MemoryContext(snapshot=None, facts=[])

    async def save_exchange(*args, **kwargs):
        return 48

    monkeypatch.setattr(harness_module, "model_gateway", MisattributedProvider())
    monkeypatch.setattr(harness_module, "load_conversation_history", empty_history)
    monkeypatch.setattr(harness_module, "load_memory_context", empty_memory)
    monkeypatch.setattr(harness_module, "create_exchange", save_exchange)

    result = asyncio.run(RuntimeHarness(retriever=EvidenceRetriever()).run(
        session, user_id="hard-fact-user", message="奖学金申请测试资料是什么",
        conversation_id=None, grade="大二", college="计算机学院", major=None,
    ))

    assert "无法从所引资料核实" in result.answer
    assert "二年级以上" not in result.answer
    assert result.citations == []
    assert result.can_create_task is False
    attempts = [item for item in session.added if isinstance(item, ModelAttempt)]
    assert len(attempts) == 1 and attempts[0].status == "SUCCEEDED"


def test_literal_grade_and_amount_are_allowed_when_present_in_cited_chunk() -> None:
    item = EvidenceRetriever().search("query", top_k=5).evidence[0]
    supported = replace(item, text="二年级以上学生可申请，金额5000元。")
    assert RuntimeHarness._unsupported_hard_facts("二年级以上学生每年5000元[1]", [supported]) == []
