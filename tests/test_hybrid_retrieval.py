from pathlib import Path

from app.retrieval.hybrid import HybridRetriever


def build_retriever(tmp_path: Path) -> HybridRetriever:
    return HybridRetriever(chroma_path=tmp_path / "chroma")


def test_scholarship_query_detects_version_conflict(tmp_path: Path) -> None:
    result = build_retriever(tmp_path).search(
        "今年国家励志奖学金到底是五千还是六千", top_k=6
    )
    ids = {item.chunk_id for item in result.evidence}
    assert "sqxy_2025_aid_policy:scholarship_amount_process" in ids
    assert "sqxy_older_national_aid_guide:inspirational_eligibility_old" in ids
    assert result.conflict_detected is True
    assert result.warnings


def test_transfer_query_returns_process_with_scope(tmp_path: Path) -> None:
    result = build_retriever(tmp_path).search("转专业在哪里申请，需要哪些步骤", top_k=3)
    process = next(
        item
        for item in result.evidence
        if item.chunk_id == "sqxy_2024_transfer_notice:transfer_process"
    )
    assert "2024级" in process.scope
    assert process.collection_status == "STALE"


def test_retrieval_modes_are_explicit(tmp_path: Path) -> None:
    retriever = build_retriever(tmp_path)
    for mode in ("vector", "bm25", "hybrid"):
        assert retriever.search("助学金怎么申请", mode=mode).evidence


def test_irrelevant_query_does_not_force_policy_evidence(tmp_path: Path) -> None:
    result = build_retriever(tmp_path).search("量子火箭发动机的涡轮叶片", top_k=5)
    assert result.evidence == []
