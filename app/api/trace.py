import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models import ActionTask, BlackboardEvent, ConversationRun, ModelAttempt, RetrievalRun
from app.schemas.trace import (
    BlackboardEventTrace,
    ModelAttemptTrace,
    RetrievalTrace,
    RunTraceResponse,
    TaskTrace,
)


router = APIRouter(prefix="/runs", tags=["trace"])


@router.get("/{run_id}", response_model=RunTraceResponse)
async def get_run_trace(
    run_id: str,
    user_id: str = Query(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_db_session),
) -> RunTraceResponse:
    try:
        async with session.begin():
            run = await session.scalar(
                select(ConversationRun).where(
                    ConversationRun.id == run_id,
                    ConversationRun.user_id == user_id.strip(),
                )
            )
            if run is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "运行记录不存在或无权访问")
            retrievals = list(
                (await session.scalars(select(RetrievalRun).where(RetrievalRun.run_id == run_id))).all()
            )
            attempts = list(
                (
                    await session.scalars(
                        select(ModelAttempt)
                        .where(ModelAttempt.run_id == run_id)
                        .order_by(ModelAttempt.id)
                    )
                ).all()
            )
            tasks = list(
                (await session.scalars(select(ActionTask).where(ActionTask.run_id == run_id))).all()
            )
            events = list((await session.scalars(
                select(BlackboardEvent)
                .where(BlackboardEvent.run_id == run_id)
                .order_by(BlackboardEvent.sequence)
            )).all())
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法读取运行追踪") from exc

    return RunTraceResponse(
        run_id=run.id,
        user_id=run.user_id,
        conversation_id=run.conversation_id,
        intent=run.intent,
        risk_level=run.risk_level,
        status=run.status,
        blackboard=json.loads(run.blackboard_json),
        blackboard_events=[BlackboardEventTrace(
            sequence=item.sequence,
            agent=item.agent,
            event_type=item.event_type,
            payload=json.loads(item.payload_json),
        ) for item in events],
        retrievals=[
            RetrievalTrace(
                query=item.query,
                conflict_detected=item.conflict_detected,
                results=json.loads(item.result_json),
            )
            for item in retrievals
        ],
        model_attempts=[
            ModelAttemptTrace(
                attempt_no=index,
                provider=item.provider,
                operation=item.operation,
                status=item.status,
                latency_ms=item.latency_ms,
                prompt_tokens=item.prompt_tokens,
                completion_tokens=item.completion_tokens,
                total_tokens=item.total_tokens,
                error_code=item.error_code,
            )
            for index, item in enumerate(attempts, start=1)
        ],
        tasks=[TaskTrace(task_id=item.id, status=item.status, attempts=item.attempts) for item in tasks],
        error_code=run.error_code,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )
