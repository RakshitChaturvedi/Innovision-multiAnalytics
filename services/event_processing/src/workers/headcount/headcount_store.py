import uuid
import time
from datetime import datetime

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from .config import config


class HeadcountStore:

    def __init__(self):
        self._engine = create_async_engine(
            config.DATABASE_URL
        )

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

        self._redis: aioredis.Redis | None = None
        
        # In-memory tracking of last snapshot write per zone to throttle writes
        self._last_snapshot_write: dict[str, float] = {}

    async def initialize(
        self,
        redis_client: aioredis.Redis,
    ):
        self._redis = redis_client

    async def update_rolling_average(
        self,
        zone_id: str,
        count: int,
        timestamp: datetime,
    ) -> float:
        """
        Pushes count to a sorted set, removes entries older than window,
        and calculates rolling average.
        """
        key = f"{config.HEADCOUNT_CACHE_PREFIX}:{zone_id}:window"
        now_ts = timestamp.timestamp()
        
        async with self._redis.pipeline(transaction=True) as pipe:
            # Add new count with timestamp as score
            pipe.zadd(key, {str(count) + ":" + str(uuid.uuid4()): now_ts})
            # Remove older entries
            cutoff = now_ts - config.ROLLING_WINDOW_SECONDS
            pipe.zremrangebyscore(key, "-inf", cutoff)
            # Fetch remaining items
            pipe.zrange(key, 0, -1)
            # Set TTL to prevent stale data buildup
            pipe.expire(key, config.HEADCOUNT_CACHE_TTL)
            
            results = await pipe.execute()
            
        items = results[2]  # Output of zrange
        
        if not items:
            return float(count)
            
        total_count = sum(int(item.decode().split(":")[0]) for item in items)
        return total_count / len(items)

    async def should_write_snapshot(self, zone_id: str) -> bool:
        """Throttles snapshot writes based on interval."""
        now = time.time()
        last_write = self._last_snapshot_write.get(zone_id, 0.0)
        
        if now - last_write >= config.SNAPSHOT_INTERVAL_SECONDS:
            self._last_snapshot_write[zone_id] = now
            return True
            
        return False

    async def write_snapshot(
        self,
        camera_id: str,
        zone_id: str,
        count: int,
        rolling_avg: float,
        timestamp: datetime,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO headcount_snapshots (
                            id,
                            camera_id,
                            zone_id,
                            count,
                            rolling_avg,
                            timestamp
                        )
                        VALUES (
                            CAST(:id AS uuid),
                            CAST(:camera_id AS uuid),
                            CAST(:zone_id AS uuid),
                            :count,
                            :rolling_avg,
                            :timestamp
                        )
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "camera_id": camera_id,
                        "zone_id": zone_id,
                        "count": count,
                        "rolling_avg": rolling_avg,
                        "timestamp": timestamp,
                    },
                )

    async def write_breach_event(
        self,
        zone_id: str,
        camera_id: str,
        count: int,
        threshold: int,
        timestamp: datetime,
    ) -> uuid.UUID:
        event_id = uuid.uuid4()
        
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO headcount_breach_events (
                            id,
                            zone_id,
                            camera_id,
                            count,
                            threshold,
                            alert_status,
                            timestamp
                        )
                        VALUES (
                            CAST(:id AS uuid),
                            CAST(:zone_id AS uuid),
                            CAST(:camera_id AS uuid),
                            :count,
                            :threshold,
                            'pending',
                            :timestamp
                        )
                        """
                    ),
                    {
                        "id": str(event_id),
                        "zone_id": zone_id,
                        "camera_id": camera_id,
                        "count": count,
                        "threshold": threshold,
                        "timestamp": timestamp,
                    },
                )
                
        return event_id

    async def get_open_breach(
        self,
        zone_id: str,
    ) -> dict | None:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        id,
                        alert_status
                    FROM headcount_breach_events
                    WHERE zone_id = CAST(:zone_id AS uuid)
                      AND alert_status != 'resolved'
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """
                ),
                {
                    "zone_id": zone_id,
                },
            )

            row = result.fetchone()

            if row is None:
                return None

            return {
                "id": str(row.id),
                "alert_status": row.alert_status,
            }

    async def resolve_breach_event(
        self,
        event_id: str,
        timestamp: datetime,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE headcount_breach_events
                        SET
                            alert_status = 'resolved',
                            resolved_at = :timestamp
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {
                        "id": event_id,
                        "timestamp": timestamp,
                    },
                )
