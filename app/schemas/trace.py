from datetime import datetime
from typing import Any

from pydantic import BaseModel


class RetrievalTrace(BaseModel):
    query: str
    conflict_detected: bool
    results: list[dict[str, Any]]


class ModelAttemptTrace(BaseModel):
    attempt_no: int
    provider: str
    operation: str
    status: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    error_code: str | None


class TaskTrace(BaseModel):
    task_id: str
    status: str
    attempts: int


class BlackboardEventTrace(BaseModel):
    sequence: int
    agent: str
    event_type: str
    payload: dict[str, Any]


class RunTraceResponse(BaseModel):
    run_id: str
    user_id: str
    conversation_id: int | None
    intent: str
    risk_level: str
    status: str
    blackboard: dict[str, Any]
    blackboard_events: list[BlackboardEventTrace]
    retrievals: list[RetrievalTrace]
    model_attempts: list[ModelAttemptTrace]
    tasks: list[TaskTrace]
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None
