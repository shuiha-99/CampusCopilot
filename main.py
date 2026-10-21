from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy import text

from app.api.chat import router as chat_router
from app.api.memory import router as memory_router
from app.api.agent import router as agent_router
from app.api.actions import router as actions_router
from app.api.trace import router as trace_router
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory, engine
from app.retrieval.hybrid import DEFAULT_CHROMA_PATH


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await redis_client.aclose()
        await engine.dispose()


app = FastAPI(
    title="Campus Copilot",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


@app.get("/", include_in_schema=False)
def demo_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# 校园最小路径
@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness_check() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
    except Exception:
        checks["mysql"] = "unavailable"
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except RedisError:
        checks["redis"] = "degraded"
    checks["chroma"] = "ok" if DEFAULT_CHROMA_PATH.exists() else "not_indexed"
    ready = checks["mysql"] == "ok" and checks["chroma"] == "ok"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(agent_router)
app.include_router(actions_router)
app.include_router(trace_router)
