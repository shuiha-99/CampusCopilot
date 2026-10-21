import re


_LATIN_OR_NUMBER = re.compile(r"[a-zA-Z0-9_.-]+")
_CJK = re.compile(r"[\u3400-\u9fff]")


def tokenize(text: str) -> list[str]:
    """为中文 BM25 和本地嵌入生成稳定 token，不依赖在线分词服务。"""
    lowered = text.lower()
    latin = _LATIN_OR_NUMBER.findall(lowered)
    chinese = _CJK.findall(lowered)
    bigrams = ["".join(chinese[index : index + 2]) for index in range(len(chinese) - 1)]
    return latin + chinese + bigrams
