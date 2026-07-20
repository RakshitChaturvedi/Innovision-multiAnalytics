from __future__ import annotations

import redis.asyncio as aioredis

from services.ingestion.src.config import settings
from shared.schemas import FrameEvent

class FramePublisher:
    def __init__(self) -> None:
        self._redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True
        )
    
    async def publish(self, event: FrameEvent) -> None:
        await self._redis.xadd(
            name=settings.frame_stream_name,
            fields=event.model_dump(mode="json"),
            maxlen=settings.frame_stream_maxlen,
            approximate=True
        )
    
    async def close(self) -> None:
        await self._redis.aclose()