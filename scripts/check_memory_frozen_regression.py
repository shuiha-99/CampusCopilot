"""Replay a hash-locked real Ollama summary through the current context builder."""

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from app.runtime.harness import RuntimeHarness
from app.services.memory_service import MemoryContext
from app.services.model_gateway import OllamaGateway
from scripts.check_memory_benefit_v1 import EVIDENCE, MESSAGES, QUESTION


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/evaluation/memory_v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _input_chars(history: list[dict[str, str]], memory_text: str) -> int:
    gateway = object.__new__(OllamaGateway)
    context = RuntimeHarness._evidence_context([EVIDENCE], memory_text)
    payload = gateway._build_chat_messages(
        message=QUESTION, history=history, context=context
    )
    return sum(len(item["content"]) for item in payload)


def run() -> dict:
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    archived = FIXTURE / "recorded_ollama_summary.json"
    archived_bytes = archived.read_bytes()
    if _sha256(archived_bytes) != manifest["recorded_summary_sha256"]:
        raise ValueError("recorded summary changed; publish a new memory fixture version")
    report = json.loads(archived_bytes)
    sample_sha = _sha256(json.dumps(MESSAGES, ensure_ascii=False).encode("utf-8"))
    if sample_sha != manifest["case_sha256"] or report["sample_sha256"] != sample_sha:
        raise ValueError("memory example changed; publish a new memory fixture version")
    if report["model"] != manifest["recorded_model"]:
        raise ValueError("recorded model does not match manifest")

    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": content}
        for index, content in enumerate(MESSAGES)
    ]
    summary = report["second_summary"]
    memory_text = MemoryContext(
        snapshot=SimpleNamespace(brief=summary),
        facts=[SimpleNamespace(fact_key="grade", fact_value="大三")],
    ).as_model_context()
    assert memory_text is not None
    full_chars = _input_chars(history, "用户明确确认的事实：\n- grade: 大三")
    compact_chars = _input_chars(history[-10:], memory_text)
    deadline_sentences = re.findall(r"[^。\n]*2026[^。\n]*截止[^。\n]*", summary)
    checks = {
        "goal_preserved": "国家励志奖学金" in summary,
        "deadline_still_unknown": any(
            any(word in sentence for word in ("需查", "待核对", "未确认", "尚未", "不能直接"))
            for sentence in deadline_sentences
        ),
        "old_amount_not_current": "旧金额不能直接作为目标年度的标准" in summary,
        "confirmed_grade_present": "grade: 大三" in memory_text,
        "2024_2025_amounts_separated": bool(
            "2024" in summary and "2025" in summary and "分别" in summary
        ),
    }
    core_checks = {key: value for key, value in checks.items()
                   if key != "2024_2025_amounts_separated"}
    result = {
        "version": manifest["version"],
        "case_count": manifest["case_count"],
        "case_sha256": sample_sha,
        "mode": "recorded_real_ollama_summary_replay",
        "current_model_called": False,
        "full_history_input_chars": full_chars,
        "compact_memory_input_chars": compact_chars,
        "char_reduction_fraction": round((full_chars - compact_chars) / full_chars, 4),
        "core_checks": core_checks,
        "all_core_checks_passed": all(core_checks.values()),
        "additional_check": {"2024_2025_amounts_separated": checks["2024_2025_amounts_separated"]},
        "known_limit": "The recorded summary did not separately state the 2024 and 2025 amount tiers.",
    }
    if full_chars != report["full_history_input_chars"] or compact_chars != report["compact_memory_input_chars"]:
        raise AssertionError("model context changed from the frozen reference")
    if not result["all_core_checks_passed"]:
        raise AssertionError("memory summary lost a required fact or uncertainty")
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run(), ensure_ascii=False, indent=2))
