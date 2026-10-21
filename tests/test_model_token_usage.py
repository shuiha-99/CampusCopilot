import asyncio
from types import SimpleNamespace

from app.core.config import Settings
from app.models import ModelAttempt
from app.runtime.harness import RuntimeHarness
from app.services.model_gateway import (
    ModelRouter,
    OllamaGateway,
    TokenUsage,
)


class FakeCompletions:
    async def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="可核实回答[1]"))],
            usage=SimpleNamespace(
                prompt_tokens=123, completion_tokens=7, total_tokens=130
            ),
        )


def test_router_records_provider_reported_usage_on_success() -> None:
    settings = Settings(model_provider="mock")
    gateway = OllamaGateway(settings)
    gateway._client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    router = ModelRouter([("ollama", gateway)], settings)
    attempts = []

    async def observe(attempt):
        attempts.append(attempt)

    answer = asyncio.run(router.generate("奖学金条件？", attempt_observer=observe))

    assert answer == "可核实回答[1]"
    assert len(attempts) == 1
    assert attempts[0].usage == TokenUsage(123, 7, 130)


def test_missing_usage_stays_unknown_instead_of_becoming_zero() -> None:
    settings = Settings(model_provider="mock")
    gateway = OllamaGateway(settings)

    class NoUsageCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="回答[1]"))],
                usage=None,
            )

    gateway._client = SimpleNamespace(
        chat=SimpleNamespace(completions=NoUsageCompletions())
    )
    router = ModelRouter([("ollama", gateway)], settings)
    attempts = []

    async def observe(attempt):
        attempts.append(attempt)

    asyncio.run(router.generate("测试", attempt_observer=observe))

    assert len(attempts) == 1
    assert attempts[0].usage is None


def test_provider_token_usage_is_saved_with_model_attempt() -> None:
    class Session:
        def __init__(self):
            self.records = []

        def begin(self):
            class Transaction:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            return Transaction()

        def add(self, item):
            self.records.append(item)

    session = Session()
    asyncio.run(RuntimeHarness._record_model_attempt(
        session, run_id="run", provider="ollama", operation="answer",
        status="SUCCEEDED", latency_ms=25, error_code=None,
        usage=TokenUsage(123, 7, 130),
    ))

    record = session.records[0]
    assert isinstance(record, ModelAttempt)
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (123, 7, 130)
