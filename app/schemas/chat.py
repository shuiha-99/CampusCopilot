from pydantic import BaseModel, Field, field_validator


# 限制输入
class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1, max_length=500, description="学生提出的校园政策或办事问题"
    )
    user_id: str = Field(min_length=1, max_length=64, description="当前演示用户标识")
    conversation_id: int | None = Field(
        default=None,
        gt=0,
        description="继续已有会话时提供；首次对话留空",
    )

    # 先清除首尾空格，再执行长度校验，避免纯空格问题进入业务层
    @field_validator("user_id", "message", mode="before")
    @classmethod
    def strip_message(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value


# 限制输出
class ChatResponse(BaseModel):
    answer: str
    evidence_available: bool
    conversation_id: int
