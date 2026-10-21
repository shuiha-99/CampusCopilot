class PersistenceError(Exception):
    """持久化会话或消息时发生的项目级异常。"""


class ConversationNotFoundError(PersistenceError):
    """会话不存在，或不属于当前用户。"""
