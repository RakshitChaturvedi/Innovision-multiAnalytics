from __future__ import annotations

import asyncio
import redis.asyncio as aioredis
from datetime import datetime, UTC

from services.ingestion.src.config import settings

class HeartbeatPublisher:
    def __init__(self) -> None:
        self._redis = aioredis.from_url(
            settings.redis_url,
            decode_responses = True,
        )
        self._running = False

    async def run(self) ->None:
        self._running = True
        while self._running:
            await self._publish()
            await asyncio.sleep(settings.heartbeat_interval_seconds)

    async def stop(self) -> None:
        self._running = False
    
    async def close(self) -> None:
        await self._redis.aclose()
    
    async def _publish(self) -> None:
        await self._redis.xadd(
            name="heartbeat",
            fields={
                "service": settings.service_name,
                "camera_id": settings.camera_id,
                "status": "running",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            approximate=True
        )