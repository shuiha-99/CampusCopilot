from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ActionDraftRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    conversation_id: int | None = Field(default=None, gt=0)
    run_id: str | None = Field(default=None, max_length=36)
    action_type: Literal["scholarship_preparation", "major_transfer_preparation"]
    details: dict[str, Any]

    @field_validator("user_id", mode="before")
    @classmethod
    def strip_user_id(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ActionDraftResponse(BaseModel):
    draft_id: str
    action_type: str
    summary: str
    confirmation_token: str
    expires_at: datetime
    warning: str


class ActionConfirmRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=36, max_length=36)
    confirmation_token: str = Field(min_length=20, max_length=200)


class TaskResponse(BaseModel):
    task_id: str
    status: str
    action_type: str
    attempts: int
    result: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class WorkerRunResponse(BaseModel):
    processed: bool
    task: TaskResponse | None


class ManualDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("必须填写操作原因")
        return value


class ManualTaskResponse(TaskResponse):
    user_id: str
    run_id: str | None
    payload: dict[str, Any]
    retry_attempts: int
    generation: int


class ManualOperationResponse(BaseModel):
    operator_id: str
    operation: str
    before_status: str
    after_status: str
    reason: str
    attempts_before: int
    generation_before: int
    created_at: datetime


class ManualTaskDetailResponse(ManualTaskResponse):
    operations: list[ManualOperationResponse]
