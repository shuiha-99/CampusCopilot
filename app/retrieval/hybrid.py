from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from rank_bm25 import BM25Okapi

from app.core.config import settings
from app.retrieval.embedding import HashingEmbedding, OllamaEmbedding
from app.retrieval.tokenizer import tokenize


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_FILE = PROJECT_ROOT / "data" / "knowledge" / "sources.json"
DEFAULT_CHROMA_PATH = PROJECT_ROOT / "data" / "chroma"


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    source_key: str
    title: str
    url: str
    text: str
    scope: str
    collection_status: str
    validity: str
    version_label: str
    published_at: str
    score: float
    vector_score: float
    bm25_score: float


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    evidence: list[Evidence]
    conflict_detected: bool
    warnings: list[str]


class HybridRetriever:
    """Chroma 向量召回与 BM25 关键词召回的本地混合检索。"""

    def __init__(
        self,
        *,
        source_file: Path = DEFAULT_SOURCE_FILE,
        chroma_path: Path = DEFAULT_CHROMA_PATH,
        vector_weight: float = 0.55,
        bm25_weight: float = 0.45,
        embedding_keep_alive: int | str = 0,
    ) -> None:
        self.source_file = source_file
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        if settings.embedding_provider == "ollama":
            self.embedding = OllamaEmbedding(
                base_url=settings.ollama_base_url,
                model=settings.ollama_embedding_model,
                timeout_seconds=settings.model_timeout_seconds,
                keep_alive=embedding_keep_alive,
            )
            collection_name = "campus_policy_ollama_" + hashlib.sha256(
                settings.ollama_embedding_model.encode("utf-8")
            ).hexdigest()[:12]
        else:
            self.embedding = HashingEmbedding()
            collection_name = "campus_policy_hash_v1"
        self.client = chromadb.PersistentClient(path=str(chroma_path))
        self.collection = self.client.get_or_create_collection(
            collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._records = self._load_records()
        self._by_id = {record["chunk_id"]: record for record in self._records}
        self._bm25 = BM25Okapi([tokenize(record["text"]) for record in self._records])
        self._vector_score_cache: dict[tuple[str, int], dict[str, float]] = {}

    def _load_records(self) -> list[dict[str, Any]]:
        sources = json.loads(self.source_file.read_text(encoding="utf-8"))
        records: list[dict[str, Any]] = []
        for source in sources:
            source_hash = hashlib.sha256(
                json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            for position, chunk in enumerate(source["chunks"]):
                records.append(
                    {
                        "chunk_id": f"{source['source_key']}:{chunk['chunk_key']}",
                        "source_key": source["source_key"],
                        "title": source["title"],
                        "url": source["url"],
                        "published_at": source["published_at"],
                        "reviewed_at": source["reviewed_at"],
                        "collection_status": source["collection_status"],
                        "validity": source["validity"],
                        "scope": source["scope"],
                        "version_label": source["version_label"],
                        "source_hash": source_hash,
                        "position": position,
                        "text": chunk["text"],
                    }
                )
        return records

    def index(self) -> int:
        if not self._records:
            return 0
        current_ids = {record["chunk_id"] for record in self._records}
        indexed_ids = set(self.collection.get(include=[])["ids"])
        removed_ids = sorted(indexed_ids - current_ids)
        if removed_ids:
            self.collection.delete(ids=removed_ids)
        self.collection.upsert(
            ids=[record["chunk_id"] for record in self._records],
            documents=[record["text"] for record in self._records],
            embeddings=self.embedding([record["text"] for record in self._records]),
            metadatas=[
                {
                    key: value
                    for key, value in record.items()
                    if key not in {"chunk_id", "text"}
                }
                for record in self._records
            ],
        )
        return len(self._records)

    def search(
        self, query: str, *, top_k: int = 5, mode: str = "hybrid"
    ) -> RetrievalResult:
        if mode not in {"vector", "bm25", "hybrid"}:
            raise ValueError("mode 必须是 vector、bm25 或 hybrid")
        if self.collection.count() != len(self._records):
            self.index()

        candidate_count = min(max(top_k * 3, top_k), len(self._records))
        vector_scores: dict[str, float] = {}
        if mode != "bm25":
            cache_key = (query, candidate_count)
            cached_scores = self._vector_score_cache.get(cache_key)
            if cached_scores is None:
                vector = self.collection.query(
                    query_embeddings=[self.embedding.embed(query)],
                    n_results=candidate_count,
                    include=["distances"],
                )
                cached_scores = {
                    chunk_id: max(0.0, 1.0 - float(distance))
                    for chunk_id, distance in zip(
                        vector["ids"][0], vector["distances"][0]
                    )
                }
                self._vector_score_cache[cache_key] = cached_scores
            vector_scores = cached_scores

        raw_bm25 = self._bm25.get_scores(tokenize(query))
        query_terms = {token for token in tokenize(query) if len(token) >= 2}
        max_bm25 = max((float(score) for score in raw_bm25), default=0.0)
        bm25_scores = {
            record["chunk_id"]: (float(raw_bm25[index]) / max_bm25 if max_bm25 > 0 else 0.0)
            for index, record in enumerate(self._records)
        }

        ranked: list[Evidence] = []
        for record in self._records:
            chunk_id = record["chunk_id"]
            vector_score = vector_scores.get(chunk_id, 0.0)
            bm25_score = bm25_scores[chunk_id]
            record_terms = {token for token in tokenize(record["text"]) if len(token) >= 2}
            has_meaningful_overlap = bool(query_terms & record_terms)
            if mode == "vector":
                score = vector_score
            elif mode == "bm25":
                score = bm25_score
            else:
                score = self.vector_weight * vector_score + self.bm25_weight * bm25_score
            if not has_meaningful_overlap:
                score = 0.0
            if record["collection_status"] == "STALE":
                score *= 0.92
            ranked.append(
                Evidence(
                    chunk_id=chunk_id,
                    source_key=record["source_key"],
                    title=record["title"],
                    url=record["url"],
                    text=record["text"],
                    scope=record["scope"],
                    collection_status=record["collection_status"],
                    validity=record["validity"],
                    version_label=record["version_label"],
                    published_at=record["published_at"],
                    score=round(score, 6),
                    vector_score=round(vector_score, 6),
                    bm25_score=round(bm25_score, 6),
                )
            )

        selected = [
            item
            for item in sorted(ranked, key=lambda item: item.score, reverse=True)
            if item.score >= 0.12
        ][:top_k]
        warnings: list[str] = []
        if any(item.collection_status == "STALE" for item in selected):
            warnings.append("检索结果包含历史资料，涉及当前办理日期或金额时必须复核最新通知。")
        conflict_detected = self._detect_amount_conflict(selected)
        if conflict_detected:
            warnings.append("不同版本资料中的奖助金额不一致，系统不会自动把发布日期较新的页面判定为当前有效政策。")
        return RetrievalResult(
            query=query,
            evidence=selected,
            conflict_detected=conflict_detected,
            warnings=warnings,
        )

    @staticmethod
    def _detect_amount_conflict(evidence: list[Evidence]) -> bool:
        award_texts = [item.text for item in evidence if "奖学金" in item.text and "元" in item.text]
        amounts: set[str] = set()
        for text in award_texts:
            amounts.update(part for part in ("5000", "6000", "8000", "10000") if part in text)
        return ("5000" in amounts and "6000" in amounts) or (
            "8000" in amounts and "10000" in amounts
        )
