"""Measure a fixed memory example against real MySQL; no generation model is called."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select

from app.cache.conversation_cache import build_history_key
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory, engine
from app.models import ChatMessage, Conversation, ConversationRun, MemorySnapshot
from app.retrieval.hybrid import Evidence, RetrievalResult
from app.runtime import harness as harness_module
from app.runtime.harness import RuntimeHarness
from app.services.conversation_service import load_conversation_history
from app.services.memory_service import (
    compact_memory,
    confirm_fact,
    load_memory_context,
    revoke_fact,
)
from app.services.model_gateway import DeepSeekGateway
from main import app


MESSAGES = [
    "目标：了解国家励志奖学金的申请流程，只依据学校公开资料。",
    "我会注明资料来源和发布日期，并区分历史资料与现行通知。",
    "我特别关心2026年的申请截止日，旧通知不能直接当作今年日期。",
    "目前资料不足以确认2026年截止日，需要查当年的学校通知。",
    "还想比较2024年和2025年的助学金档次，先看各自年份。",
    "比较时会分别标注年份，不把旧金额直接套到新年份。",
    "学校公示通常在哪一步发生？请不要把推测写成规定。",
    "会查公开章程中的流程表述，再说明没有覆盖的环节。",
    "如果资料版本之间金额不同，请把差异提醒出来。",
    "会保留两个版本和来源，让你复核当年办理要求。",
    "我正在准备材料，但还没有向学校提交任何申请。",
    "内部准备清单不代表学校已经受理或审批。",
    "我希望先得到材料核对清单，之后再决定是否创建任务。",
    "只有你显式确认后，系统才会创建内部准备任务。",
    "申请资格请结合年级和学院，不要只凭专业名称判断。",
    "会把缺少的适用范围作为未知项，不自行补造资格。",
    "请把2025年的资助资料与2024年的资料分开引用。",
    "会按页面发布日期和适用范围列出来源。",
    "如果今年政策没更新，就明确说现有资料不足。",
    "会把旧页面作为历史参考，而不是今年的有效依据。",
    "我仍想知道申请流程，但不要替我判断最终获批。",
    "会说明能核实的流程，最终资格和结果以学校为准。",
    "当前先复习政策问答和办事准备两条流程。",
    "好的，后续答复会沿用已确认的目标和限制。",
]
QUESTION = "请概括我关心的奖学金申请问题，并说明目前还有哪些未知项？"
EVIDENCE = Evidence(
    chunk_id="fixed:policy", source_key="fixed", title="固定对照资料",
    url="https://example.test/fixed", text="奖助申请需核对当年通知与适用范围。",
    scope="固定样例", collection_status="ACTIVE", validity="UNKNOWN",
    version_label="memory-v1", published_at="2025-01-01", score=1.0,
    vector_score=1.0, bm25_score=1.0,
)
BRIEF_V1 = "用户目标：了解国家励志奖学金申请流程；2026年截止日尚无可靠通知；旧年份金额不可直接用于今年。"
BRIEF_V2 = BRIEF_V1 + " 用户尚未提交申请，希望先核对材料；资料冲突需列明来源和年份。"


class FixedSummarizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[dict[str, str]]]] = []

    async def summarize(self, *, previous_brief, messages):
        self.calls.append((previous_brief, messages))
        return BRIEF_V1 if previous_brief is None else BRIEF_V2


class FailingSummarizer:
    async def summarize(self, *, previous_brief, messages):
        raise RuntimeError("injected summary failure")


class FixedRetriever:
    def search(self, query, *, top_k):
        return RetrievalResult(
            query=query, evidence=[EVIDENCE], conflict_detected=False, warnings=[]
        )


class RecordingGenerator:
    def __init__(self) -> None:
        self.context = None

    async def generate(self, message, *, history=None, context=None):
        self.context = context
        return "请核对当年通知与适用范围[1]。"


def input_chars(history: list[dict[str, str]], context: str) -> int:
    gateway = object.__new__(DeepSeekGateway)
    model_messages = gateway._build_chat_messages(
        message=QUESTION, history=history, context=context
    )
    return sum(len(item["content"]) for item in model_messages)


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    user_id = "memory-benefit-" + uuid4().hex[:10]
    conversation_id = None
    sample_sha = hashlib.sha256(
        json.dumps(MESSAGES, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    summarizer = FixedSummarizer()

    async with async_session_factory() as session:
        try:
            async with session.begin():
                conversation = Conversation(user_id=user_id)
                session.add(conversation)
                await session.flush()
                conversation_id = conversation.id
                session.add_all(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="user" if index % 2 == 0 else "assistant",
                        content=content,
                    )
                    for index, content in enumerate(MESSAGES[:16])
                )

            failed = False
            try:
                await compact_memory(
                    session, user_id=user_id, conversation_id=conversation_id,
                    summarizer=FailingSummarizer(),
                )
            except RuntimeError:
                failed = True
            assert failed
            async with session.begin():
                count_after_failure = await session.scalar(
                    select(func.count(MemorySnapshot.id)).where(
                        MemorySnapshot.conversation_id == conversation_id
                    )
                )
            assert count_after_failure == 0

            first = await compact_memory(
                session, user_id=user_id, conversation_id=conversation_id,
                summarizer=summarizer,
            )
            assert first is not None and first.version == 1
            assert [item["content"] for item in summarizer.calls[0][1]] == MESSAGES[:6]

            async with session.begin():
                session.add_all(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="user" if index % 2 == 0 else "assistant",
                        content=content,
                    )
                    for index, content in enumerate(MESSAGES[16:], start=16)
                )
            second = await compact_memory(
                session, user_id=user_id, conversation_id=conversation_id,
                summarizer=summarizer,
            )
            assert second is not None and second.version == 2
            assert summarizer.calls[1][0] == BRIEF_V1
            assert [item["content"] for item in summarizer.calls[1][1]] == MESSAGES[6:14]
            assert await compact_memory(
                session, user_id=user_id, conversation_id=conversation_id,
                summarizer=summarizer,
            ) is None

            await confirm_fact(
                session, user_id=user_id, conversation_id=conversation_id,
                key="grade", value="大二",
            )
            await confirm_fact(
                session, user_id=user_id, conversation_id=conversation_id,
                key="grade", value="大三",
            )
            await confirm_fact(
                session, user_id=user_id, conversation_id=conversation_id,
                key="college", value="计算机学院",
            )
            await revoke_fact(
                session, user_id=user_id, conversation_id=conversation_id, key="college"
            )
            memory = await load_memory_context(
                session, user_id=user_id, conversation_id=conversation_id
            )
            recent = await load_conversation_history(
                session, user_id=user_id, conversation_id=conversation_id
            )
            assert memory.snapshot is not None and memory.snapshot.version == 2
            assert len(recent) == 10 and recent[0]["content"] == MESSAGES[14]
            assert [(fact.fact_key, fact.fact_value) for fact in memory.facts] == [
                ("grade", "大三")
            ]
            memory_text = memory.as_model_context()
            assert memory_text is not None
            for retained in ("国家励志奖学金", "2026年截止日", "大三"):
                assert retained in memory_text
            assert "计算机学院" not in memory_text

            full_history = [
                {"role": "user" if index % 2 == 0 else "assistant", "content": content}
                for index, content in enumerate(MESSAGES)
            ]
            active_fact_text = "用户明确确认的事实：\n- grade: 大三"
            full_input = input_chars(
                full_history,
                RuntimeHarness._evidence_context([EVIDENCE], active_fact_text),
            )
            compact_input = input_chars(
                recent,
                RuntimeHarness._evidence_context([EVIDENCE], memory_text),
            )
            generator = RecordingGenerator()
            previous_gateway = harness_module.model_gateway
            try:
                harness_module.model_gateway = generator
                agent_result = await RuntimeHarness(retriever=FixedRetriever()).run(
                    session, user_id=user_id,
                    message="奖助申请需核对当年通知吗？",
                    conversation_id=conversation_id, grade="大三",
                    college=None, major=None,
                )
            finally:
                harness_module.model_gateway = previous_gateway
            assert BRIEF_V2 in generator.context and "grade: 大三" in generator.context
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    f"/runs/{agent_result.run_id}", params={"user_id": user_id}
                )
            response.raise_for_status()
            trace_memory = response.json()["blackboard"]["memory"]
            assert trace_memory["snapshot_id"] == second.id
            assert trace_memory["snapshot_version"] == 2
            assert trace_memory["active_fact_keys"] == ["grade"]
            assert trace_memory["used_in_model_context"] is True
            report = {
                "sample_version": "memory-v1-fixed-example",
                "sample_sha256": sample_sha,
                "measurement": "sum of content character lengths in built model messages; not tokens or billed usage",
                "real_generation_model_called": False,
                "injected_generator_called": bool(generator.context),
                "summarizer": "deterministic fixed fixture, not an LLM quality test",
                "message_count": len(MESSAGES),
                "recent_message_count": len(recent),
                "snapshot_versions": [first.version, second.version],
                "v1_new_messages": len(summarizer.calls[0][1]),
                "v2_new_messages": len(summarizer.calls[1][1]),
                "failure_left_snapshot_count": count_after_failure,
                "retry_created_v1": first.version == 1,
                "active_facts_after_update_revoke": {
                    fact.fact_key: fact.fact_value for fact in memory.facts
                },
                "retained_checks": ["国家励志奖学金", "2026年截止日", "大三"],
                "full_history_input_chars": full_input,
                "compact_memory_input_chars": compact_input,
                "char_reduction_fraction": round((full_input - compact_input) / full_input, 4),
                "trace_snapshot_id_matches": trace_memory["snapshot_id"] == second.id,
                "trace_snapshot_version": trace_memory["snapshot_version"],
                "trace_memory_used_in_model_context": trace_memory["used_in_model_context"],
                "trace_validation": "real MySQL and in-process HTTP; injected answer generator",
            }
            output = Path("data/evaluation/memory_v1_results.json")
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False))
        finally:
            await session.rollback()
            if conversation_id is not None:
                async with session.begin():
                    await session.execute(
                        delete(ConversationRun).where(ConversationRun.user_id == user_id)
                    )
                    await session.execute(
                        delete(Conversation).where(Conversation.id == conversation_id)
                    )
                try:
                    await redis_client.delete(build_history_key(user_id, conversation_id))
                except RedisError:
                    pass
    await redis_client.aclose()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
