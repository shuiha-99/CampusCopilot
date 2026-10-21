from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AgentQueryRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    conversation_id: int | None = Field(default=None, gt=0)
    grade: str | None = Field(default=None, max_length=32)
    college: str | None = Field(default=None, max_length=100)
    major: str | None = Field(default=None, max_length=100)

    @field_validator("user_id", "message", "grade", "college", "major", mode="before")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class CitationResponse(BaseModel):
    source_key: str
    title: str
    url: str
    scope: str
    collection_status: Literal["ACTIVE", "STALE"]
    validity: str
    version_label: str
    published_at: str
    score: float


class AgentQueryResponse(BaseModel):
    run_id: str
    conversation_id: int
    intent: str
    risk_level: str
    answer: str
    citations: list[CitationResponse]
    unknowns: list[str]
    warnings: list[str]
    conflict_detected: bool
    can_create_task: bool
