"""
Per-camera recognition configuration (similarity threshold, min face
size, blur threshold, sample rate), cached in memory.

Invalidated either explicitly via invalidate(), or live via the
cache:camera_config:invalidate Pub/Sub channel once initialize() is
called with a redis client — mirrors EmbeddingCache's invalidation
pattern, so threshold tuning takes effect without a service restart.
"""
import asyncio
import logging

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import config

logger = logging.getLogger(__name__)


class CameraConfigStore:
    def __init__(self) -> None:
        self._engine = create_async_engine(config.DATABASE_URL)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._cache: dict[str, dict] = {}
        self._redis: aioredis.Redis | None = None

    async def initialize(self, redis_client: aioredis.Redis) -> None:
        """Optional — enables live cache invalidation via Pub/Sub."""
        self._redis = redis_client
        asyncio.create_task(self._listen_invalidations())

    async def get(self, camera_id: str) -> dict:
        if camera_id in self._cache:
            return self._cache[camera_id]

        async with self._session_factory() as session:
            row = await session.execute(text("""
                SELECT similarity_threshold, min_face_size_px,
                       blur_threshold, recognition_sample_rate
                FROM camera_config
                WHERE camera_id = :camera_id
            """), {"camera_id": camera_id})
            result = row.fetchone()

        if result:
            cfg = {
                "similarity_threshold": result.similarity_threshold,
                "min_face_size_px": result.min_face_size_px,
                "blur_threshold": result.blur_threshold,
                "recognition_sample_rate": result.recognition_sample_rate,
            }
        else:
            cfg = {
                "similarity_threshold": config.VISITOR_THRESHOLD,
                "min_face_size_px": config.DEFAULT_MIN_FACE_SIZE_PX,
                "blur_threshold": config.DEFAULT_BLUR_THRESHOLD,
                "recognition_sample_rate": config.DEFAULT_SAMPLE_RATE,
            }

        self._cache[camera_id] = cfg
        return cfg

    def invalidate(self, camera_id: str) -> None:
        self._cache.pop(camera_id, None)

    async def _listen_invalidations(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(config.CAMERA_CONFIG_INVALIDATE_CHANNEL)
        logger.info(
            "camera_config_store_listening channel=%s",
            config.CAMERA_CONFIG_INVALIDATE_CHANNEL,
        )

        async for message in pubsub.listen():
            if message["type"] == "message":
                camera_id = message["data"]
                if isinstance(camera_id, bytes):
                    camera_id = camera_id.decode()
                self.invalidate(camera_id)
                logger.info("camera_config_invalidated camera_id=%s", camera_id)
