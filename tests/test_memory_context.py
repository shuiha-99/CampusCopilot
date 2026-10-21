from types import SimpleNamespace

from app.services.memory_service import MemoryContext


def test_memory_context_separates_summary_and_confirmed_facts() -> None:
    context = MemoryContext(
        snapshot=SimpleNamespace(brief="用户正在了解国家励志奖学金。"),
        facts=[
            SimpleNamespace(fact_key="college", fact_value="计算机学院"),
            SimpleNamespace(fact_key="grade", fact_value="大二"),
        ],
    )

    assert context.as_model_context() == (
        "较早对话摘要：\n用户正在了解国家励志奖学金。\n\n"
        "用户明确确认的事实：\n- college: 计算机学院\n- grade: 大二"
    )


def test_empty_memory_context_is_not_sent_to_model() -> None:
    assert MemoryContext(snapshot=None, facts=[]).as_model_context() is None
