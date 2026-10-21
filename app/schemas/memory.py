from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class FactConfirmRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    value: str = Field(min_length=1, max_length=500)

    @field_validator("user_id", "key", "value", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class MemoryCompactRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)

    @field_validator("user_id", mode="before")
    @classmethod
    def strip_user_id(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class FactResponse(BaseModel):
    key: str
    value: str
    active: bool


class SnapshotResponse(BaseModel):
    version: int
    brief: str
    covered_message_id: int
    input_chars: int
    output_chars: int
    created_at: datetime


class MemoryStateResponse(BaseModel):
    snapshot: SnapshotResponse | None
    facts: list[FactResponse]


class CompactResponse(BaseModel):
    created: bool
    snapshot: SnapshotResponse | None
