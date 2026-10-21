import json

from app.retrieval import HybridRetriever


def main() -> None:
    retriever = HybridRetriever()
    count = retriever.index()
    print(json.dumps({"indexed_chunks": count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
