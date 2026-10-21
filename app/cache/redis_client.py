from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.maint_notifications import MaintNotificationsConfig
from redis.retry import Retry

from app.core.config import settings

redis_client = Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    decode_responses=True,
    socket_connect_timeout=settings.redis_connect_timeout_seconds,
    socket_timeout=settings.redis_connect_timeout_seconds,
    retry=Retry(NoBackoff(), 0),
    # Memurai 4.1 提供项目所需的 Redis 命令，但不兼容 redis-py 8.1
    # 自动启用的 RESP3 维护通知握手。项目不依赖该通知，固定 RESP2 即可。
    protocol=2,
    maint_notifications_config=MaintNotificationsConfig(enabled=False),
)
