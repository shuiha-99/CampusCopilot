import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.services.model_errors import ModelConnectionError
from app.services.model_gateway import CircuitBreaker, CircuitState, ModelRouter, OllamaGateway


def test_local_generation_has_separate_timeout_from_cloud_provider() -> None:
    settings = Settings(
        model_provider="mock", model_timeout_seconds=7,
        ollama_generation_timeout_seconds=90,
    )
    assert OllamaGateway(settings)._client.timeout == 90


class FailingProvider:
    async def generate(self, *args, **kwargs):
        raise ModelConnectionError("primary unavailable")

    async def stream_generate(self, *args, **kwargs):
        if False:
            yield "never"
        raise ModelConnectionError("primary unavailable")

    async def summarize(self, **kwargs):
        raise ModelConnectionError("primary unavailable")


class WorkingProvider:
    async def generate(self, *args, **kwargs):
        return "fallback answer"

    async def stream_generate(self, *args, **kwargs):
        yield "fallback"

    async def summarize(self, **kwargs):
        return "fallback summary"


def build_router() -> ModelRouter:
    settings = Settings(model_provider="mock")
    return ModelRouter(
        [("primary", FailingProvider()), ("fallback", WorkingProvider())], settings
    )


def test_router_falls_back_before_output() -> None:
    assert asyncio.run(build_router().generate("hello")) == "fallback answer"


def test_router_reports_each_actual_provider_attempt_in_order() -> None:
    attempts = []

    async def observe(attempt):
        attempts.append(attempt)

    assert asyncio.run(
        build_router().generate("hello", attempt_observer=observe)
    ) == "fallback answer"
    assert [(item.provider, item.operation, item.status, item.error_code) for item in attempts] == [
        ("primary", "answer", "FAILED", "ModelConnectionError"),
        ("fallback", "answer", "SUCCEEDED", None),
    ]
    assert all(item.latency_ms >= 0 for item in attempts)


def test_open_breaker_skips_provider_without_recording_an_attempt() -> None:
    router = build_router()
    router.breakers["primary"].state = CircuitState.OPEN
    router.breakers["primary"].opened_at = datetime.now(timezone.utc)
    attempts = []

    async def observe(attempt):
        attempts.append(attempt)

    assert asyncio.run(router.generate("hello", attempt_observer=observe)) == "fallback answer"
    assert [(item.provider, item.status) for item in attempts] == [("fallback", "SUCCEEDED")]


def test_stream_router_falls_back_before_first_token() -> None:
    async def collect():
        return [part async for part in build_router().stream_generate("hello")]

    assert asyncio.run(collect()) == ["fallback"]


def test_stream_router_never_splices_after_output() -> None:
    class PartialProvider(FailingProvider):
        async def stream_generate(self, *args, **kwargs):
            yield "partial"
            raise ModelConnectionError("broken after first token")

    settings = Settings(model_provider="mock")
    router = ModelRouter(
        [("primary", PartialProvider()), ("fallback", WorkingProvider())], settings
    )

    attempts = []

    async def observe(attempt):
        attempts.append(attempt)

    async def collect():
        return [part async for part in router.stream_generate("hello", attempt_observer=observe)]

    with pytest.raises(ModelConnectionError):
        asyncio.run(collect())
    assert [(item.provider, item.status) for item in attempts] == [("primary", "FAILED")]


def test_circuit_breaker_transitions_open_half_open_closed() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=1)
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow_request() is False
    breaker.opened_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    assert breaker.allow_request() is True
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
