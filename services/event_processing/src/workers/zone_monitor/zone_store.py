import asyncio
import json
import logging

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from .config import config


logger = logging.getLogger(__name__)


class ZoneStore:

    def __init__(self):

        self._local_cache: dict[
            str,
            list[dict],
        ] = {}

        self._engine = create_async_engine(
            config.DATABASE_URL
        )

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

        self._redis: aioredis.Redis | None = None

    async def initialize(
        self,
        redis_client: aioredis.Redis,
    ):

        self._redis = redis_client

        asyncio.create_task(
            self._listen_invalidations()
        )

    async def get_zones(
        self,
        camera_id: str,
    ) -> list[dict]:

        # L1 cache
        if camera_id in self._local_cache:
            return self._local_cache[camera_id]

        redis_key = (
            f"{config.ZONE_CONFIG_CACHE_PREFIX}:"
            f"{camera_id}"
        )

        # L2 Redis cache
        try:

            cached = await self._redis.get(
                redis_key
            )

            if cached:

                zones = json.loads(cached)

                self._local_cache[camera_id] = zones

                return zones

        except Exception as exc:

            logger.warning(
                "zone_redis_cache_failed: %s",
                exc,
            )

        # PostgreSQL fallback
        zones = await self._load_from_db(
            camera_id
        )

        self._local_cache[camera_id] = zones

        try:

            await self._redis.setex(
                redis_key,
                config.ZONE_CONFIG_CACHE_TTL,
                json.dumps(zones),
            )

        except Exception as exc:

            logger.warning(
                "zone_redis_cache_write_failed: %s",
                exc,
            )

        return zones

    async def _load_from_db(
        self,
        camera_id: str,
    ) -> list[dict]:

        async with self._session_factory() as session:

            result = await session.execute(
                text(
                    """
                    SELECT
                        z.id,
                        z.name,
                        z.type,
                        z.polygon,
                        z.max_headcount,
                        z.dwell_threshold_seconds
                    FROM zones z
                    WHERE z.camera_id = :camera_id
                    """
                ),
                {
                    "camera_id": camera_id
                },
            )

            zones = []

            for row in result.fetchall():

                zones.append(
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "type": row.type,
                        "polygon": row.polygon,
                        "max_headcount": row.max_headcount,
                        "dwell_threshold_seconds": (
                            row.dwell_threshold_seconds
                            or config.DEFAULT_DWELL_THRESHOLD_SECONDS
                        ),
                    }
                )

            return zones

    async def invalidate(
        self,
        camera_id: str,
    ):

        self._local_cache.pop(
            camera_id,
            None,
        )

        redis_key = (
            f"{config.ZONE_CONFIG_CACHE_PREFIX}:"
            f"{camera_id}"
        )

        try:
            await self._redis.delete(
                redis_key
            )
        except Exception:
            pass

        await self.get_zones(camera_id)

    async def _listen_invalidations(self):

        pubsub = self._redis.pubsub()

        await pubsub.psubscribe(
            "cache:zone_config:*:invalidate"
        )

        async for message in pubsub.listen():

            if message["type"] != "pmessage":
                continue

            channel = message["channel"]

            if isinstance(channel, bytes):
                channel = channel.decode()

            parts = channel.split(":")

            if len(parts) >= 4:

                camera_id = parts[2]

                await self.invalidate(
                    camera_id
                )