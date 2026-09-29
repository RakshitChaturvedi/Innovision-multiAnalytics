"""
Per-camera recognition configuration (similarity threshold, min face
size, blur threshold, sample rate), cached in memory.

Entries expire after CACHE_TTL_SECONDS, so a missed invalidation heals by
itself. They are also invalidated explicitly via invalidate(), or live via the
cache:camera_config:invalidate Pub/Sub channel once initialize() is called
with a redis client. A missing camera_config table or row is not an error:
defaults are used and the situation is logged once, not once per message.
"""
import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import config
from .supervised import supervise

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0
_UNDEFINED_TABLE_SQLSTATE = "42P01"


def _is_missing_table(exc: DBAPIError) -> bool:
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        if err is not None and getattr(err, "sqlstate", None) == _UNDEFINED_TABLE_SQLSTATE:
            return True
    msg = str(exc.orig).lower()
    return "camera_config" in msg and "does not exist" in msg


class CameraConfigStore:
    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        stopping: asyncio.Event | None = None,
    ) -> None:
        # Shared with the owning consumer: set when the service stops.
        self._stopping = stopping if stopping is not None else asyncio.Event()
        self._engine = create_async_engine(config.DATABASE_URL)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}  # camera_id -> (expires_at, cfg)
        self._redis: aioredis.Redis | None = None
        self._listener_task: asyncio.Task | None = None
        self._warned_missing_table = False
        self._warned_missing_row: set[str] = set()

    async def initialize(self, redis_client: aioredis.Redis) -> None:
        """Optional — enables live cache invalidation via Pub/Sub."""
        self._redis = redis_client
        self._listener_task = asyncio.create_task(
            supervise("camera_config_listener", self._listen_invalidations, stopping=self._stopping),
            name="camera-config-listener",
        )

    async def close(self) -> None:
        self._stopping.set()  # a listener dying from here on is not restarted
        task, self._listener_task = self._listener_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._engine.dispose()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "similarity_threshold": config.VISITOR_THRESHOLD,
            "min_face_size_px": config.DEFAULT_MIN_FACE_SIZE_PX,
            "blur_threshold": config.DEFAULT_BLUR_THRESHOLD,
            "recognition_sample_rate": config.DEFAULT_SAMPLE_RATE,
        }

    async def get(self, camera_id: str) -> dict:
        entry = self._cache.get(camera_id)
        if entry is not None and self._clock() < entry[0]:
            return entry[1]

        cfg = await self._load(camera_id)
        self._cache[camera_id] = (self._clock() + self._ttl, cfg)
        return cfg

    async def _load(self, camera_id: str) -> dict:
        try:
            async with self._session_factory() as session:
                row = await session.execute(text("""
                    SELECT similarity_threshold, min_face_size_px,
                           blur_threshold, recognition_sample_rate
                    FROM camera_config
                    WHERE camera_id = :camera_id
                """), {"camera_id": camera_id})
                result = row.fetchone()
        except DBAPIError as exc:
            if not _is_missing_table(exc):
                raise  # connection trouble etc.: transient, let the message retry
            if not self._warned_missing_table:
                self._warned_missing_table = True
                logger.warning("camera_config_table_missing using_defaults_for_all_cameras")
            return self._defaults()

        if result is None:
            if camera_id not in self._warned_missing_row:
                self._warned_missing_row.add(camera_id)
                logger.warning("camera_config_row_missing camera_id=%s using_defaults", camera_id)
            return self._defaults()

        defaults = self._defaults()
        return {
            "similarity_threshold": result.similarity_threshold if result.similarity_threshold is not None else defaults["similarity_threshold"],
            "min_face_size_px": result.min_face_size_px if result.min_face_size_px is not None else defaults["min_face_size_px"],
            "blur_threshold": result.blur_threshold if result.blur_threshold is not None else defaults["blur_threshold"],
            "recognition_sample_rate": result.recognition_sample_rate if result.recognition_sample_rate is not None else defaults["recognition_sample_rate"],
        }

    def invalidate(self, camera_id: str) -> None:
        self._cache.pop(camera_id, None)

    async def _listen_invalidations(self) -> None:
        """One subscription session; the supervisor restarts it on any failure."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(config.CAMERA_CONFIG_INVALIDATE_CHANNEL)
            logger.info(
                "camera_config_store_listening channel=%s",
                config.CAMERA_CONFIG_INVALIDATE_CHANNEL,
            )
            # Anything published while we were not subscribed is lost.
            self._cache.clear()

            async for message in pubsub.listen():
                if message["type"] == "message":
                    camera_id = message["data"]
                    if isinstance(camera_id, bytes):
                        camera_id = camera_id.decode()
                    self.invalidate(camera_id)
                    logger.info("camera_config_invalidated camera_id=%s", camera_id)
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                logger.warning("camera_config_pubsub_close_failed", exc_info=True)
