import redis.asyncio as aioredis

from shared.frame_cache.base import FrameCache
from shared.frame_cache.exceptions import (
    FrameCacheConnectionError,
    FrameCacheOperationError
)

class RedisFrameCache(FrameCache):
    # redis implementation of FrameCache interface

    def __init__(self, redis_url: str) -> None:
        self._redis = aioredis.from_url(
            redis_url,
            decode_response=False
        )
    
    @staticmethod
    def _key(frame_reference: str) -> str:
        return f"frames:{frame_reference}"
    
    async def put(
            self, frame_reference: str,
            frame_bytes: bytes, ttl_seconds: int,
    ) -> None:
        try:
            await self._redis.set(
                self._key(frame_reference),
                frame_bytes,
                ex=ttl_seconds,
            )
        except Exception as exc:
            raise FrameCacheOperationError("Failed to store frame in cache.") from exc
        
    async def get(self, frame_reference: str) -> bytes | None:
        try:
            return await self._redis.get(self._key(frame_reference))
        except Exception as exc:
            raise FrameCacheOperationError("Failed to retrieve frame from cache.") from exc
    
    async def exists(self, frame_reference: str) -> bool:
        try:
            return bool(
                await self._redis.exists(self._key(frame_reference))
            )
        except Exception as exc:
            raise FrameCacheOperationError("Failed to check frame existence.") from exc
    
    async def delete(self, frame_reference: str) -> None:
        try:
            await self._redis.delete(self._key(frame_reference))
        except Exception as exc:
            raise FrameCacheOperationError("Failed to delete frame.") from exc
    
    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception as exc:
            raise FrameCacheConnectionError("Failed to close redis connection.") from exc