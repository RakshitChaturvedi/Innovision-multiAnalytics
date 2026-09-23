import json
from datetime import datetime

import redis.asyncio as aioredis

from .config import config


class TrackStateStore:

    def __init__(
        self,
        redis_client: aioredis.Redis,
    ):
        self._redis = redis_client

    async def get_current_zones(
        self,
        camera_id: str,
        track_id: int,
    ) -> set[str]:

        key = (
            f"{config.TRACK_STATE_PREFIX}:"
            f"{camera_id}:{track_id}"
        )

        try:
            raw = await self._redis.get(key)

            if not raw:
                return set()

            return set(json.loads(raw))

        except Exception:
            return set()

    async def set_current_zones(
        self,
        camera_id: str,
        track_id: int,
        zone_ids: set[str],
    ):

        key = (
            f"{config.TRACK_STATE_PREFIX}:"
            f"{camera_id}:{track_id}"
        )

        await self._redis.setex(
            key,
            config.TRACK_STATE_TTL,
            json.dumps(list(zone_ids)),
        )

    async def get_entry_time(
        self,
        camera_id: str,
        track_id: int,
        zone_id: str,
    ) -> datetime | None:

        key = (
            f"{config.ENTRY_TIME_PREFIX}:"
            f"{camera_id}:{track_id}:{zone_id}"
        )

        try:
            raw = await self._redis.get(key)

            if not raw:
                return None

            if isinstance(raw, bytes):
                raw = raw.decode()

            return datetime.fromisoformat(raw)

        except Exception:
            return None

    async def set_entry_time(
        self,
        camera_id: str,
        track_id: int,
        zone_id: str,
        timestamp: datetime,
    ):

        key = (
            f"{config.ENTRY_TIME_PREFIX}:"
            f"{camera_id}:{track_id}:{zone_id}"
        )

        await self._redis.setex(
            key,
            config.ENTRY_TIME_TTL,
            timestamp.isoformat(),
        )

    async def delete_entry_time(
        self,
        camera_id: str,
        track_id: int,
        zone_id: str,
    ):

        key = (
            f"{config.ENTRY_TIME_PREFIX}:"
            f"{camera_id}:{track_id}:{zone_id}"
        )

        await self._redis.delete(key)