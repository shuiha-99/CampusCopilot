import asyncio

import httpx2
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
)

from app.core.config import Settings
from app.services.model_errors import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelGatewayError,
    ModelResponseError,
    ModelTimeoutError,
)
from app.services.model_gateway import DeepSeekGateway


class RaisingCompletions:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create(self, **kwargs):
        raise self._error


class FakeChat:
    def __init__(self, error: Exception) -> None:
        self.completions = RaisingCompletions(error)


class FakeClient:
    def __init__(self, error: Exception) -> None:
        self.chat = FakeChat(error)


request = httpx2.Request(
    "POST",
    "https://api.deepseek.com/chat/completions",
)

authentication_response = httpx2.Response(
    401,
    request=request,
)


@pytest.mark.parametrize(
    ("sdk_error", "expected_error_type"),
    [
        (
            APITimeoutError(request=request),
            ModelTimeoutError,
        ),
        (
            APIConnectionError(request=request),
            ModelConnectionError,
        ),
        (
            AuthenticationError(
                "测试认证失败",
                response=authentication_response,
                body=None,
            ),
            ModelAuthenticationError,
        ),
    ],
)
def test_deepseek_gateway_converts_sdk_error(
    monkeypatch,
    sdk_error: Exception,
    expected_error_type: type[ModelGatewayError],
) -> None:
    settings = Settings(
        model_provider="deepseek",
        deepseek_api_key="test-key",
    )
    gateway = DeepSeekGateway(settings)

    monkeypatch.setattr(
        gateway,
        "_client",
        FakeClient(sdk_error),
    )

    with pytest.raises(expected_error_type):
        asyncio.run(gateway.generate("测试问题"))


async def collect_stream(gateway: DeepSeekGateway) -> list[str]:
    return [part async for part in gateway.stream_generate("测试问题")]


@pytest.mark.parametrize(
    ("sdk_error", "expected_error_type"),
    [
        (APITimeoutError(request=request), ModelTimeoutError),
        (APIConnectionError(request=request), ModelConnectionError),
        (
            AuthenticationError(
                "测试认证失败",
                response=authentication_response,
                body=None,
            ),
            ModelAuthenticationError,
        ),
    ],
)
def test_deepseek_stream_converts_sdk_error(
    monkeypatch,
    sdk_error: Exception,
    expected_error_type: type[ModelGatewayError],
) -> None:
    settings = Settings(
        model_provider="deepseek",
        deepseek_api_key="test-key",
    )
    gateway = DeepSeekGateway(settings)
    monkeypatch.setattr(gateway, "_client", FakeClient(sdk_error))

    with pytest.raises(expected_error_type):
        asyncio.run(collect_stream(gateway))


class EmptyStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class ReturningCompletions:
    async def create(self, **kwargs):
        return EmptyStream()


class ReturningClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": ReturningCompletions()})()


def test_deepseek_stream_rejects_empty_response(monkeypatch) -> None:
    settings = Settings(
        model_provider="deepseek",
        deepseek_api_key="test-key",
    )
    gateway = DeepSeekGateway(settings)
    monkeypatch.setattr(gateway, "_client", ReturningClient())

    with pytest.raises(ModelResponseError):
        asyncio.run(collect_stream(gateway))
