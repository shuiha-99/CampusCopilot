import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, get_db_session
from app.api.manual_auth import get_manual_operator
from app.mcp_server import invoke_create_preparation_task
from app.schemas.action import (
    ActionConfirmRequest,
    ActionDraftRequest,
    ActionDraftResponse,
    ManualDecisionRequest,
    ManualOperationResponse,
    ManualTaskDetailResponse,
    ManualTaskResponse,
    TaskResponse,
    WorkerRunResponse,
)
from app.services.action_service import (
    ActionValidationError,
    ConfirmationError,
    ManualTaskConflict,
    ManualTaskNotFound,
    authorize_draft_confirmation,
    build_preparation_result,
    create_action_draft,
    finish_task,
    get_task,
    inspect_manual_task,
    lease_one_task,
    list_manual_tasks,
    decide_manual_task,
)
from app.services.persistence_errors import ConversationNotFoundError, PersistenceError


router = APIRouter(prefix="/actions", tags=["actions"])


def _task_response(task) -> TaskResponse:
    return TaskResponse(
        task_id=task.id,
        status=task.status,
        action_type=task.action_type,
        attempts=task.attempts,
        result=json.loads(task.result_json) if task.result_json else None,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _manual_task_response(task) -> ManualTaskResponse:
    return ManualTaskResponse(
        **_task_response(task).model_dump(),
        user_id=task.user_id,
        run_id=task.run_id,
        payload=json.loads(task.payload_json),
        retry_attempts=task.retry_attempts,
        generation=task.generation,
    )


@router.get("/manual/tasks", response_model=list[ManualTaskResponse])
async def list_tasks_for_manual_review(
    limit: int = Query(default=50, ge=1, le=100),
    operator_id: str = Depends(get_manual_operator),
    session: AsyncSession = Depends(get_db_session),
) -> list[ManualTaskResponse]:
    del operator_id
    try:
        tasks = await list_manual_tasks(session, limit=limit)
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法读取任务") from exc
    return [_manual_task_response(task) for task in tasks]


@router.get("/manual/tasks/{task_id}", response_model=ManualTaskDetailResponse)
async def read_manual_task(
    task_id: str,
    operator_id: str = Depends(get_manual_operator),
    session: AsyncSession = Depends(get_db_session),
) -> ManualTaskDetailResponse:
    del operator_id
    try:
        task, operations = await inspect_manual_task(session, task_id=task_id)
    except ManualTaskNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法读取任务") from exc
    return ManualTaskDetailResponse(
        **_manual_task_response(task).model_dump(),
        operations=[ManualOperationResponse.model_validate(operation, from_attributes=True)
                    for operation in operations],
    )


@router.post("/manual/tasks/{task_id}/resume", response_model=ManualTaskResponse)
async def resume_manual_task(
    task_id: str,
    request: ManualDecisionRequest,
    operator_id: str = Depends(get_manual_operator),
    session: AsyncSession = Depends(get_db_session),
) -> ManualTaskResponse:
    return await _manual_decision(session, task_id, operator_id, "RESUME", request.reason)


@router.post("/manual/tasks/{task_id}/cancel", response_model=ManualTaskResponse)
async def cancel_manual_task(
    task_id: str,
    request: ManualDecisionRequest,
    operator_id: str = Depends(get_manual_operator),
    session: AsyncSession = Depends(get_db_session),
) -> ManualTaskResponse:
    return await _manual_decision(session, task_id, operator_id, "CANCEL", request.reason)


async def _manual_decision(
    session: AsyncSession, task_id: str, operator_id: str, operation: str, reason: str
) -> ManualTaskResponse:
    try:
        task = await decide_manual_task(
            session,
            task_id=task_id,
            operator_id=operator_id,
            operation=operation,
            reason=reason,
        )
    except ManualTaskNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ManualTaskConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法处理任务") from exc
    return _manual_task_response(task)


@router.post("/drafts", response_model=ActionDraftResponse)
async def create_draft(
    request: ActionDraftRequest,
    session: AsyncSession = Depends(get_db_session),
) -> ActionDraftResponse:
    try:
        draft, token = await create_action_draft(session, **request.model_dump())
    except ActionValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问") from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法创建草稿") from exc
    details = request.details
    summary = (
        f"为{details.get('grade')}、{details.get('college')}创建{details.get('scholarship_type')}准备清单"
        if request.action_type == "scholarship_preparation"
        else f"为{details.get('grade')}创建转入{details.get('target_major')}的准备清单"
    )
    return ActionDraftResponse(
        draft_id=draft.id,
        action_type=draft.action_type,
        summary=summary,
        confirmation_token=token,
        expires_at=draft.expires_at,
        warning="确认后只创建项目内部准备任务，不会向学校提交申请。",
    )


@router.post("/confirm", response_model=TaskResponse)
async def confirm_action(
    request: ActionConfirmRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TaskResponse:
    try:
        draft = await authorize_draft_confirmation(session, **request.model_dump())
        result = await invoke_create_preparation_task(
            draft_id=draft.id,
            run_id=draft.run_id,
            user_id=draft.user_id,
            conversation_id=draft.conversation_id,
            action_type=draft.action_type,
            payload_json=draft.payload_json,
            idempotency_key=f"confirmed:{draft.operation_hash}",
        )
        async with async_session_factory() as read_session:
            task = await get_task(
                read_session, task_id=result["task_id"], user_id=request.user_id
            )
    except ConfirmationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法创建任务") from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP 工具调用失败") from exc
    if task is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "任务创建结果不可用")
    return _task_response(task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def read_task(
    task_id: str,
    user_id: str = Query(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_db_session),
) -> TaskResponse:
    task = await get_task(session, task_id=task_id, user_id=user_id.strip())
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或无权访问")
    return _task_response(task)


@router.post("/worker/run-once", response_model=WorkerRunResponse)
async def run_worker_once() -> WorkerRunResponse:
    worker_id = "api-worker-" + uuid.uuid4().hex[:8]
    async with async_session_factory() as lease_session:
        leased = await lease_one_task(lease_session, worker_id=worker_id)
    if leased is None:
        return WorkerRunResponse(processed=False, task=None)
    task, generation = leased
    result = build_preparation_result(task)
    async with async_session_factory() as finish_session:
        committed = await finish_task(
            finish_session,
            task_id=task.id,
            worker_id=worker_id,
            generation=generation,
            result=result,
        )
    if not committed:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务租约已被其他执行者接管")
    async with async_session_factory() as read_session:
        completed = await get_task(read_session, task_id=task.id, user_id=task.user_id)
    return WorkerRunResponse(processed=True, task=_task_response(completed))
