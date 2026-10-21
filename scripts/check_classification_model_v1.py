"""Probe the real local model's structured advisory classification."""

import asyncio
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from app.core.config import settings
from app.runtime.classification import CLASSIFICATION_SYSTEM_PROMPT, ModelClassification
from app.services.model_gateway import OllamaGateway


CASES = [
    {"message": "请帮我创建奖学金准备清单", "expected_intent": "scholarship", "minimum_risk": "HIGH"},
    {"message": "我想了解转入软件工程专业要满足什么条件", "expected_intent": "major_transfer", "minimum_risk": "LOW"},
]


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    gateway = OllamaGateway(settings)
    results = []
    risk_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    for case in CASES:
        started = time.perf_counter()
        raw = await gateway.generate(
            case["message"], system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
        )
        try:
            decision = ModelClassification.model_validate_json(raw)
            structure_valid = True
            intent_matches = decision.intent == case["expected_intent"]
            risk_not_below_expected = (
                risk_order[decision.risk_level] >= risk_order[case["minimum_risk"]]
            )
            parsed = decision.model_dump()
        except ValidationError:
            structure_valid = False
            intent_matches = False
            risk_not_below_expected = False
            parsed = None
        results.append({
            **case, "raw": raw, "parsed": parsed,
            "structure_valid": structure_valid, "intent_matches": intent_matches,
            "risk_not_below_expected": risk_not_below_expected,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        })
    report = {"provider": "ollama", "model": settings.ollama_model, "cases": results}
    output = Path("data/evaluation/classification_model_v1_ollama.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
