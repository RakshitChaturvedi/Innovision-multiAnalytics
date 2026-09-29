import asyncio
import json
import logging
import math
import time

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from .config import config
from .supervisor import supervise


logger = logging.getLogger(__name__)

INVALIDATION_PATTERN = (
    f"{config.ZONE_CONFIG_CACHE_PREFIX}:*:invalidate"
)


def invalidation_channel(camera_id: str) -> str:
    return f"{config.ZONE_CONFIG_CACHE_PREFIX}:{camera_id}:invalidate"


async def publish_zone_invalidation(
    redis_client: aioredis.Redis,
    camera_id: str,
) -> int:
    """
    Tell every service that caches zones to drop `camera_id`.
    For seed/admin scripts. Returns the number of subscribers reached.
    """

    return await redis_client.publish(
        invalidation_channel(str(camera_id)), "1"
    )


def _coordinate(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or not 0.0 <= value <= 1.0:
        return None
    return float(value)


def validate_polygon(polygon) -> list[list[float]] | None:
    """
    Normalise a polygon to [[x, y], ...] with x, y in 0..1.
    Accepts [x, y] pairs or {"x":..., "y":...} dicts (or a JSON string of
    either). Returns None if it has < 3 points or any invalid point.
    """

    if isinstance(polygon, (str, bytes)):
        try:
            polygon = json.loads(polygon)
        except ValueError:
            return None

    if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
        return None

    points = []

    for point in polygon:
        if isinstance(point, dict):
            x, y = point.get("x"), point.get("y")
        elif isinstance(point, (list, tuple)) and len(point) == 2:
            x, y = point
        else:
            return None

        x, y = _coordinate(x), _coordinate(y)

        if x is None or y is None:
            return None

        points.append([x, y])

    return points


class ZoneStore:

    def __init__(
        self,
        session_factory=None,
        clock=time.monotonic,
    ):

        # camera_id -> (expires_at, zones)
        self._local_cache: dict[
            str,
            tuple[float, list[dict]],
        ] = {}

        self._clock = clock
        self._warned_zones: set[str] = set()

        if session_factory is None:
            engine = create_async_engine(config.DATABASE_URL)
            session_factory = async_sessionmaker(
                engine,
                expire_on_commit=False,
            )

        self._session_factory = session_factory

        self._redis: aioredis.Redis | None = None
        self._listener_task: asyncio.Task | None = None

    async def initialize(
        self,
        redis_client: aioredis.Redis,
    ):

        self._redis = redis_client

        self._listener_task = asyncio.create_task(
            supervise(
                "zone_invalidation_listener",
                self._listen_invalidations,
            )
        )

    async def close(self):
        if self._listener_task:
            self._listener_task.cancel()
            await asyncio.gather(
                self._listener_task, return_exceptions=True
            )

    def _cache_put(self, camera_id: str, zones: list[dict]):
        self._local_cache[camera_id] = (
            self._clock() + config.ZONE_CACHE_TTL_S,
            zones,
        )

    async def get_zones(
        self,
        camera_id: str,
    ) -> list[dict]:

        # L1 cache (TTL)
        entry = self._local_cache.get(camera_id)

        if entry and entry[0] > self._clock():
            return entry[1]

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

                self._cache_put(camera_id, zones)

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

        self._cache_put(camera_id, zones)

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

            rows = result.fetchall()

        zones = []

        for row in rows:

            polygon = validate_polygon(row.polygon)

            if polygon is None:
                zone_id = str(row.id)

                if zone_id not in self._warned_zones:
                    self._warned_zones.add(zone_id)
                    logger.warning(
                        "zone_invalid_polygon_skipped camera=%s zone=%s",
                        camera_id,
                        zone_id,
                    )
                continue

            zones.append(
                {
                    "id": str(row.id),
                    "name": row.name,
                    "type": row.type,
                    "polygon": polygon,
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
        """Drop cached zones; they reload lazily on the next get_zones."""

        self._local_cache.pop(camera_id, None)

        # Let a still-invalid zone warn again after an admin edit.
        self._warned_zones.clear()

        await self._redis.delete(
            f"{config.ZONE_CONFIG_CACHE_PREFIX}:{camera_id}"
        )

    async def _listen_invalidations(self):

        pubsub = self._redis.pubsub()

        try:
            await pubsub.psubscribe(INVALIDATION_PATTERN)

            async for message in pubsub.listen():

                if message["type"] != "pmessage":
                    continue

                channel = message["channel"]

                if isinstance(channel, bytes):
                    channel = channel.decode()

                # cache:zone_config:{camera_id}:invalidate
                parts = channel.split(":")

                if len(parts) >= 4:
                    await self.invalidate(parts[2])
        finally:
            await pubsub.aclose()
