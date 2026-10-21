from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.retrieval import RetrievalError
from app.runtime.harness import runtime_harness
from app.schemas.agent import AgentQueryRequest, AgentQueryResponse, CitationResponse
from app.services.model_errors import ModelGatewayError
from app.services.persistence_errors import ConversationNotFoundError, PersistenceError


router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/query", response_model=AgentQueryResponse)
async def query_agent(
    request: AgentQueryRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AgentQueryResponse:
    try:
        result = await runtime_harness.run(session, **request.model_dump())
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在或无权访问") from exc
    except RetrievalError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "政策检索服务暂时不可用") from exc
    except ModelGatewayError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "模型服务暂时不可用") from exc
    except PersistenceError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "暂时无法完成请求") from exc

    return AgentQueryResponse(
        run_id=result.run_id,
        conversation_id=result.conversation_id,
        intent=result.intent,
        risk_level=result.risk_level,
        answer=result.answer,
        citations=[
            CitationResponse(
                source_key=item.source_key,
                title=item.title,
                url=item.url,
                scope=item.scope,
                collection_status=item.collection_status,
                validity=item.validity,
                version_label=item.version_label,
                published_at=item.published_at,
                score=item.score,
            )
            for item in result.citations
        ],
        unknowns=result.unknowns,
        warnings=result.warnings,
        conflict_detected=result.conflict_detected,
        can_create_task=result.can_create_task,
    )
