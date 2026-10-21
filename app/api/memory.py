from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.memory import (
    CompactResponse,
    FactConfirmRequest,
    FactResponse,
    MemoryCompactRequest,
    MemoryStateResponse,
    SnapshotResponse,
)
from app.services.memory_service import (
    compact_memory,
    confirm_fact,
    load_memory_context,
    revoke_fact,
)
from app.services.model_errors import ModelGatewayError
from app.services.model_gateway import model_gateway
from app.services.persistence_errors import ConversationNotFoundError, PersistenceError

router = APIRouter(prefix="/conversations", tags=["memory"])


def _snapshot_response(snapshot) -> SnapshotResponse | None:
    if snapshot is None:
        return None
    return SnapshotResponse(
        version=snapshot.version,
        brief=snapshot.brief,
        covered_message_id=snapshot.covered_message_id,
        input_chars=snapshot.input_chars,
        output_chars=snapshot.output_chars,
        created_at=snapshot.created_at,
    )


@router.get("/{conversation_id}/memory", response_model=MemoryStateResponse)
async def get_memory(
    conversation_id: int,
    user_id: str = Query(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_db_session),
) -> MemoryStateResponse:
    try:
        context = await load_memory_context(
            session,
            user_id=user_id.strip(),
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问") from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法读取记忆") from exc
    return MemoryStateResponse(
        snapshot=_snapshot_response(context.snapshot),
        facts=[
            FactResponse(key=fact.fact_key, value=fact.fact_value, active=fact.is_active)
            for fact in context.facts
        ],
    )


@router.post("/{conversation_id}/facts", response_model=FactResponse)
async def put_fact(
    conversation_id: int,
    request: FactConfirmRequest,
    session: AsyncSession = Depends(get_db_session),
) -> FactResponse:
    try:
        fact = await confirm_fact(
            session,
            user_id=request.user_id,
            conversation_id=conversation_id,
            key=request.key,
            value=request.value,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问") from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法保存事实") from exc
    return FactResponse(key=fact.fact_key, value=fact.fact_value, active=fact.is_active)


@router.delete("/{conversation_id}/facts/{key}", response_model=FactResponse)
async def delete_fact(
    conversation_id: int,
    key: str,
    user_id: str = Query(min_length=1, max_length=64),
    session: AsyncSession = Depends(get_db_session),
) -> FactResponse:
    try:
        fact = await revoke_fact(
            session,
            user_id=user_id.strip(),
            conversation_id=conversation_id,
            key=key,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问") from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法撤销事实") from exc
    if fact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到生效中的确认事实")
    return FactResponse(key=fact.fact_key, value=fact.fact_value, active=fact.is_active)


@router.post("/{conversation_id}/memory/compact", response_model=CompactResponse)
async def compact(
    conversation_id: int,
    request: MemoryCompactRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CompactResponse:
    try:
        snapshot = await compact_memory(
            session,
            user_id=request.user_id,
            conversation_id=conversation_id,
            summarizer=model_gateway,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问") from exc
    except ModelGatewayError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法生成记忆摘要") from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法保存记忆摘要") from exc
    return CompactResponse(created=snapshot is not None, snapshot=_snapshot_response(snapshot))
