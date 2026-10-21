import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings


def get_manual_operator(authorization: str | None = Header(default=None)) -> str:
    configured = settings.manual_operator_token
    token = configured.get_secret_value() if configured is not None else ""
    if len(token) < 32:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "人工操作凭证未配置"
        )
    prefix = "Bearer "
    supplied = (
        authorization[len(prefix) :]
        if authorization is not None and authorization.startswith(prefix)
        else ""
    )
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "人工操作凭证无效")
    return settings.manual_operator_id
