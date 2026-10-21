class RetrievalError(Exception):
    """政策检索暂时无法完成。"""


class EmbeddingServiceError(RetrievalError):
    """无法从 Embedding 服务取得有效向量。"""
