import json
from collections.abc import AsyncIterator
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from app.services.model_errors import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelGatewayError,
    ModelResponseError,
    ModelTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory, get_db_session
from app.services.conversation_service import (
    create_exchange,
    load_conversation_history,
)

from app.services.model_gateway import model_gateway
from app.services.memory_service import load_memory_context

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.persistence_errors import (
    ConversationNotFoundError,
    PersistenceError,
)

router = APIRouter(tags=["chat"])


def _raise_model_http_error(error: ModelGatewayError) -> NoReturn:
    if isinstance(error, ModelTimeoutError):
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="模型响应超时，请稍后重试",
        ) from error
    if isinstance(error, ModelConnectionError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时无法连接模型服务，请稍后重试",
        ) from error
    if isinstance(error, ModelAuthenticationError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="模型服务暂时不可用",
        ) from error
    if isinstance(error, ModelResponseError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="模型服务返回了无效结果",
        ) from error
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="模型服务暂时不可用",
    ) from error


def _model_stream_error(error: ModelGatewayError) -> tuple[str, str, bool]:
    if isinstance(error, ModelTimeoutError):
        return "model_timeout", "模型响应中断，请稍后重试", True
    if isinstance(error, ModelConnectionError):
        return "model_connection_error", "模型连接中断，请稍后重试", True
    if isinstance(error, ModelAuthenticationError):
        return "model_authentication_error", "模型服务暂时不可用", False
    return "model_response_error", "模型返回内容中断，请稍后重试", True


def _sse_event(event: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest, session: AsyncSession = Depends(get_db_session)
) -> ChatResponse:
    try:
        history = await load_conversation_history(
            session,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问",
        ) from exc
    except PersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时无法读取对话，请稍后重试",
        ) from exc
    try:
        memory_context = await load_memory_context(
            session,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问",
        ) from exc
    except PersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时无法读取长期记忆，请稍后重试",
        ) from exc
    try:
        answer = await model_gateway.generate(
            request.message,
            history=history,
            context=memory_context.as_model_context(),
        )
    except (
        ModelTimeoutError,
        ModelConnectionError,
        ModelAuthenticationError,
        ModelResponseError,
    ) as exc:
        _raise_model_http_error(exc)

    try:
        conversation_id = await create_exchange(
            session,
            user_id=request.user_id,
            question=request.message,
            answer=answer,
            conversation_id=request.conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问",
        ) from exc
    except PersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时无法保存对话，请稍后重试",
        ) from exc

    return ChatResponse(
        conversation_id=conversation_id,
        answer=answer,
        evidence_available=False,
    )


@router.post("/chat/stream")
async def stream_chat(
    request: ChatRequest,
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """流式返回模型回答；完整回答生成后才持久化一问一答。"""
    try:
        history = await load_conversation_history(
            session,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
        )
        memory_context = await load_memory_context(
            session,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问",
        ) from exc
    except PersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="暂时无法读取对话上下文，请稍后重试",
        ) from exc

    model_stream = model_gateway.stream_generate(
        request.message,
        history=history,
        context=memory_context.as_model_context(),
    )

    # 先取到第一个有效片段再开始 HTTP 响应，这样首片段前失败仍能返回准确状态码。
    try:
        first_delta = await anext(model_stream)
    except StopAsyncIteration:
        _raise_model_http_error(ModelResponseError("模型没有返回流式内容"))
    except (
        ModelTimeoutError,
        ModelConnectionError,
        ModelAuthenticationError,
        ModelResponseError,
    ) as exc:
        _raise_model_http_error(exc)

    async def generate_events() -> AsyncIterator[str]:
        answer_parts = [first_delta]
        yield _sse_event("token", {"delta": first_delta})

        try:
            async for delta in model_stream:
                answer_parts.append(delta)
                yield _sse_event("token", {"delta": delta})
        except (
            ModelTimeoutError,
            ModelConnectionError,
            ModelAuthenticationError,
            ModelResponseError,
        ) as exc:
            code, message, retryable = _model_stream_error(exc)
            yield _sse_event(
                "error",
                {"code": code, "message": message, "retryable": retryable},
            )
            return

        answer = "".join(answer_parts).strip()
        try:
            # 流式响应开始后，请求依赖可能已释放，因此保存阶段使用独立数据库会话。
            async with async_session_factory() as write_session:
                conversation_id = await create_exchange(
                    write_session,
                    user_id=request.user_id,
                    question=request.message,
                    answer=answer,
                    conversation_id=request.conversation_id,
                )
        except ConversationNotFoundError:
            yield _sse_event(
                "error",
                {
                    "code": "conversation_not_found",
                    "message": "会话不存在或无权访问",
                    "retryable": False,
                },
            )
            return
        except PersistenceError:
            yield _sse_event(
                "error",
                {
                    "code": "persistence_unavailable",
                    "message": "回答已生成，但暂时无法保存，请稍后重试",
                    "retryable": True,
                },
            )
            return

        yield _sse_event(
            "done",
            {
                "conversation_id": conversation_id,
                "evidence_available": False,
            },
        )

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
