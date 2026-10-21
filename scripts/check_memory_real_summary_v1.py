"""Probe actual local summarization on the fixed memory example."""

import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.runtime.harness import RuntimeHarness
from app.services.memory_service import MemoryContext
from app.services.model_gateway import OllamaGateway
from scripts.check_memory_benefit_v1 import EVIDENCE, MESSAGES, QUESTION


def model_input_chars(gateway, history, memory_text):
    context = RuntimeHarness._evidence_context([EVIDENCE], memory_text)
    payload = gateway._build_chat_messages(
        message=QUESTION, history=history, context=context
    )
    return sum(len(item["content"]) for item in payload)


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    gateway = OllamaGateway(settings)
    samples = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": content}
        for index, content in enumerate(MESSAGES)
    ]
    started = time.perf_counter()
    first = await gateway.summarize(previous_brief=None, messages=samples[:6])
    first_seconds = round(time.perf_counter() - started, 2)
    started = time.perf_counter()
    second = await gateway.summarize(previous_brief=first, messages=samples[6:14])
    second_seconds = round(time.perf_counter() - started, 2)
    memory_text = MemoryContext(
        snapshot=SimpleNamespace(brief=second),
        facts=[SimpleNamespace(fact_key="grade", fact_value="大三")],
    ).as_model_context()
    assert memory_text is not None
    full_chars = model_input_chars(
        gateway, samples, "用户明确确认的事实：\n- grade: 大三"
    )
    compact_chars = model_input_chars(gateway, samples[14:], memory_text)
    deadline_clauses = re.findall(r"[^。\n]*2026[^。\n]*截止[^。\n]*", second)
    uncertainty_words = ("尚未", "未确认", "无法确认", "资料不足", "待确认", "需查", "需要查", "需要核对")
    checks = {
        "scholarship_goal": "国家励志奖学金" in second,
        "2026_deadline_unresolved": any(
            any(word in clause for word in uncertainty_words)
            for clause in deadline_clauses
        ),
        "2024_2025_amounts_separated": (
            "2024" in second and "2025" in second and "分别" in second
        ),
        "historical_amount_not_current_2026": bool(re.search(
            r"(?:旧|历史|2024|2025)[^。\n]*金额[^。\n]*(?:不能|不可)[^。\n]*(?:2026|目标年度|今年|当年)"
            r"|2026[^。\n]*金额[^。\n]*(?:不能|不可)[^。\n]*(?:旧|历史|2024|2025)",
            second,
        )),
        "confirmed_grade_present": "grade: 大三" in memory_text,
    }
    report = {
        "sample_version": "memory-v1-fixed-example",
        "sample_sha256": hashlib.sha256(
            json.dumps(MESSAGES, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "provider": "ollama",
        "model": settings.ollama_model,
        "first_summary_seconds": first_seconds,
        "second_summary_seconds": second_seconds,
        "first_summary": first,
        "second_summary": second,
        "retention_checks": checks,
        "deadline_clauses_checked": deadline_clauses,
        "review_note": (
            "年份分别标注不等于明确保留旧金额不可作为2026现行标准。"
            if not checks["historical_amount_not_current_2026"] else None
        ),
        "measurement": "sum of content character lengths in built model messages; not tokens or billed usage",
        "full_history_input_chars": full_chars,
        "compact_memory_input_chars": compact_chars,
        "char_reduction_fraction": round((full_chars - compact_chars) / full_chars, 4),
    }
    output = Path("data/evaluation/memory_v1_real_summary_ollama.json")
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
