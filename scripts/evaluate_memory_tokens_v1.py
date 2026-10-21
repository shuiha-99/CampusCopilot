"""Measure provider-reported token use for full history versus Memory on frozen cases."""

import asyncio
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.retrieval.hybrid import Evidence
from app.runtime.harness import RuntimeHarness
from app.services.memory_service import MemoryContext
from app.services.model_gateway import OllamaGateway, build_memory_summary_messages
from scripts.check_memory_benefit_v1 import EVIDENCE, MESSAGES, QUESTION


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/evaluation/memory_token_v1"
RESULT = FIXTURE / "results_ollama.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_cases() -> tuple[dict, list[dict]]:
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    additional_bytes = (FIXTURE / "cases.json").read_bytes()
    if _sha256(additional_bytes) != manifest["additional_cases_sha256"]:
        raise ValueError("additional Memory cases changed; publish a new fixture version")
    legacy_sha = _sha256(json.dumps(MESSAGES, ensure_ascii=False).encode("utf-8"))
    if legacy_sha != manifest["legacy_case_sha256"]:
        raise ValueError("legacy Memory case changed; publish a new fixture version")
    additional = json.loads(additional_bytes)
    cases = [{
        "id": "scholarship_2026_unknown_deadline",
        "question": QUESTION,
        "messages": MESSAGES,
        "confirmed_facts": {"grade": "大三"},
        "evidence": EVIDENCE,
        "quality_checks": {
            "topic": ["国家励志奖学金"],
            "target_year": ["2026"],
            "deadline_unknown": ["截止", "未确认|待核对|尚未|无法确认|需要核对|需查|不能直接"],
            "historical_amount_limit": ["旧|历史|2024|2025", "金额", "不能|不可|不应"],
            "year_tiers_separated": ["2024", "2025", "分别"],
        },
    }, *additional]
    if len(cases) != manifest["case_count"] or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Memory case count or identifiers do not match manifest")
    for case in cases:
        if len(case["messages"]) != 24:
            raise ValueError("every v1 Memory case must contain 24 alternating messages")
        if isinstance(case["evidence"], dict):
            source = case["evidence"]
            case["evidence"] = Evidence(
                chunk_id=case["id"] + ":source", source_key=case["id"],
                title=source["title"], url="https://example.test/" + case["id"],
                text=source["text"], scope="合成评测资料",
                collection_status="ACTIVE", validity="UNKNOWN",
                version_label=manifest["version"], published_at=source["published_at"],
                score=1.0, vector_score=1.0, bm25_score=1.0,
            )
    return manifest, cases


def _usage(response) -> dict[str, int]:
    usage = response.usage
    if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
        raise RuntimeError("Ollama compatibility response did not include token usage")
    return {
        "prompt_tokens": int(usage.prompt_tokens),
        "completion_tokens": int(usage.completion_tokens),
        "total_tokens": int(usage.total_tokens),
    }


async def _call(gateway: OllamaGateway, messages: list[dict[str, str]], max_tokens: int) -> tuple[str, dict]:
    started = time.perf_counter()
    response = await gateway._client.chat.completions.create(
        model=gateway._model, messages=messages, max_tokens=max_tokens, stream=False
    )
    content = response.choices[0].message.content if response.choices else None
    if not content or not content.strip():
        raise RuntimeError("Ollama returned an empty response")
    return content.strip(), {**_usage(response), "elapsed_seconds": round(time.perf_counter() - started, 2)}


def _facts_text(facts: dict[str, str]) -> str:
    return "用户明确确认的事实：\n" + "\n".join(
        f"- {key}: {value}" for key, value in sorted(facts.items())
    )


def _quality_checks(case: dict, summary: str) -> dict[str, bool]:
    return {
        name: all(re.search(pattern, summary) for pattern in patterns)
        for name, patterns in case["quality_checks"].items()
    }


async def _evaluate_case(gateway: OllamaGateway, case: dict) -> dict:
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": content}
        for index, content in enumerate(case["messages"])
    ]
    first, first_usage = await _call(
        gateway, build_memory_summary_messages(None, history[:6]), 250
    )
    second, second_usage = await _call(
        gateway, build_memory_summary_messages(first, history[6:14]), 250
    )
    facts = case["confirmed_facts"]
    compact_memory = MemoryContext(
        snapshot=SimpleNamespace(brief=second),
        facts=[SimpleNamespace(fact_key=key, fact_value=value)
               for key, value in sorted(facts.items())],
    ).as_model_context()
    assert compact_memory is not None
    baseline_context = RuntimeHarness._evidence_context([case["evidence"]], _facts_text(facts))
    compact_context = RuntimeHarness._evidence_context([case["evidence"]], compact_memory)
    baseline_messages = gateway._build_chat_messages(
        message=case["question"], history=history, context=baseline_context
    )
    compact_messages = gateway._build_chat_messages(
        message=case["question"], history=history[-10:], context=compact_context
    )
    _, baseline_usage = await _call(gateway, baseline_messages, 1)
    _, compact_usage = await _call(gateway, compact_messages, 1)
    saved = baseline_usage["prompt_tokens"] - compact_usage["prompt_tokens"]
    summary_tokens = first_usage["total_tokens"] + second_usage["total_tokens"]
    quality = _quality_checks(case, second)
    return {
        "id": case["id"],
        "message_count": len(history),
        "recent_message_count": 10,
        "summary_calls": [first_usage, second_usage],
        "summary_total_tokens": summary_tokens,
        "baseline_answer": baseline_usage,
        "memory_answer": compact_usage,
        "answer_prompt_tokens_saved": saved,
        "answer_prompt_reduction_fraction": round(saved / baseline_usage["prompt_tokens"], 4),
        "net_tokens_after_one_answer": saved - summary_tokens,
        "answers_until_positive_net_saving": (
            math.floor(summary_tokens / saved) + 1 if saved > 0 else None
        ),
        "quality_checks": quality,
        "all_quality_checks_passed": all(quality.values()),
        "summary_text": second,
    }


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    manifest, cases = _load_cases()
    if "--rescore" in sys.argv:
        output = json.loads(RESULT.read_text(encoding="utf-8"))
        by_id = {case["id"]: case for case in cases}
        for item in output["cases"]:
            checks = _quality_checks(by_id[item["id"]], item["summary_text"])
            item["quality_checks"] = checks
            item["all_quality_checks_passed"] = all(checks.values())
        output["quality_scoring_note"] = (
            "Rescored saved summaries without model calls; the historical amount "
            "check now recognizes the equivalent Chinese wording 不应."
        )
        RESULT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"all_quality_checks_passed": all(
            item["all_quality_checks_passed"] for item in output["cases"]
        )}, ensure_ascii=False))
        return
    if settings.ollama_model != manifest["model"]:
        raise ValueError("configured Ollama model differs from frozen experiment model")
    gateway = OllamaGateway(settings)
    results = []
    try:
        for case in cases:
            results.append(await _evaluate_case(gateway, case))
            print(f"completed {case['id']}", flush=True)
    finally:
        await gateway._client.close()
    output = {
        "version": manifest["version"],
        "model": settings.ollama_model,
        "provider": "local Ollama OpenAI-compatible API",
        "case_count": len(results),
        "usage_source": "provider response usage; not character or tokenizer estimate",
        "answer_method": "same model/question/evidence, max_tokens=1 to measure prompt tokens",
        "summary_cost_included": True,
        "cases": results,
    }
    RESULT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "case_count": len(results),
        "all_quality_checks_passed": all(item["all_quality_checks_passed"] for item in results),
        "prompt_tokens_saved": [item["answer_prompt_tokens_saved"] for item in results],
        "net_tokens_after_one_answer": [item["net_tokens_after_one_answer"] for item in results],
        "result": str(RESULT),
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
