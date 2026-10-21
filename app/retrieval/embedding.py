import hashlib
import math

import httpx

from app.retrieval.errors import EmbeddingServiceError
from app.retrieval.tokenizer import tokenize


class HashingEmbedding:
    """离线可复现的特征哈希嵌入；用于本地索引基线和故障回归。"""

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            return [value / norm for value in vector]
        return vector

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in input]


class OllamaEmbedding:
    """调用独立 Ollama embedding 模型，不复用聊天生成模型。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        keep_alive: int | str = 0,
    ) -> None:
        self.model = model
        self.endpoint = base_url.removesuffix("/v1").rstrip("/") + "/api/embed"
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive

    def embed(self, text: str) -> list[float]:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    self.endpoint,
                    json={
                        "model": self.model,
                        "input": text,
                        "truncate": True,
                        # This CPU-only 16 GB host cannot keep bge-m3 and Qwen 7B
                        # resident together. Release the embedding model promptly.
                        "keep_alive": self.keep_alive,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            embeddings = payload.get("embeddings") or []
            if not embeddings:
                raise ValueError("Ollama embedding 模型没有返回向量")
            return [float(value) for value in embeddings[0]]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingServiceError("Embedding 服务暂时不可用") from exc

    def __call__(self, input: list[str]) -> list[list[float]]:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    self.endpoint,
                    json={
                        "model": self.model,
                        "input": input,
                        "truncate": True,
                        "keep_alive": self.keep_alive,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            embeddings = payload.get("embeddings") or []
            if len(embeddings) != len(input):
                raise ValueError("Ollama embedding 模型返回的向量数量不正确")
            return [[float(value) for value in row] for row in embeddings]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingServiceError("Embedding 服务暂时不可用") from exc
