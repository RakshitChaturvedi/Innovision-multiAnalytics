import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from .config import config

_SNAPSHOT_NAMESPACE = uuid.UUID("b3a8d6a4-2f52-4c89-8d0e-5a4f6a1c9e07")


@dataclass(frozen=True)
class OpenBreach:
    id: str
    zone_id: str
    camera_id: str
    alert_published: bool


class BreachRepository(Protocol):
    async def load_open_breaches(self) -> list[OpenBreach]: ...

    async def open_breach(
        self,
        zone_id: str,
        camera_id: str,
        count: int,
        threshold: int,
        timestamp: datetime,
    ) -> OpenBreach: ...

    async def mark_alert_published(self, breach_id: str) -> None: ...

    async def resolve_breach(
        self, breach_id: str, timestamp: datetime, reason: str
    ) -> None: ...

    async def write_snapshot(
        self,
        camera_id: str,
        zone_id: str,
        count: int,
        rolling_avg: float,
        timestamp: datetime,
    ) -> None: ...


def snapshot_id(zone_id: str, timestamp: datetime, interval: float) -> uuid.UUID:
    bucket = int(timestamp.timestamp() // max(interval, 1e-6))
    return uuid.uuid5(_SNAPSHOT_NAMESPACE, f"{zone_id}:{bucket}")


class HeadcountStore:
    """Postgres implementation of BreachRepository. Never queried per frame."""

    def __init__(self):
        self._engine = create_async_engine(config.DATABASE_URL)
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def load_open_breaches(self) -> list[OpenBreach]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, zone_id, camera_id, alert_published
                    FROM headcount_breach_events
                    WHERE status = 'open'
                    """
                )
            )
            return [
                OpenBreach(
                    id=str(r.id),
                    zone_id=str(r.zone_id),
                    camera_id=str(r.camera_id),
                    alert_published=bool(r.alert_published),
                )
                for r in result.fetchall()
            ]

    async def open_breach(
        self,
        zone_id: str,
        camera_id: str,
        count: int,
        threshold: int,
        timestamp: datetime,
    ) -> OpenBreach:
        """Insert an open breach, or return the already-open one for the zone."""
        new_id = str(uuid.uuid4())
        async with self._session_factory() as session:
            async with session.begin():
                inserted = await session.execute(
                    text(
                        """
                        INSERT INTO headcount_breach_events (
                            id, zone_id, camera_id, count, threshold,
                            alert_status, status, alert_published, timestamp
                        )
                        VALUES (
                            CAST(:id AS uuid), CAST(:zone_id AS uuid),
                            CAST(:camera_id AS uuid), :count, :threshold,
                            'pending', 'open', false, :timestamp
                        )
                        ON CONFLICT (zone_id) WHERE status = 'open' DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "id": new_id,
                        "zone_id": zone_id,
                        "camera_id": camera_id,
                        "count": count,
                        "threshold": threshold,
                        "timestamp": timestamp,
                    },
                )
                if inserted.fetchone() is not None:
                    return OpenBreach(new_id, zone_id, camera_id, False)

                existing = await session.execute(
                    text(
                        """
                        SELECT id, alert_published
                        FROM headcount_breach_events
                        WHERE zone_id = CAST(:zone_id AS uuid)
                          AND status = 'open'
                        """
                    ),
                    {"zone_id": zone_id},
                )
                row = existing.fetchone()
                if row is None:
                    raise RuntimeError(
                        f"open_breach conflict but no open row zone={zone_id}"
                    )
                return OpenBreach(
                    str(row.id), zone_id, camera_id, bool(row.alert_published)
                )

    async def mark_alert_published(self, breach_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE headcount_breach_events
                        SET alert_published = true
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {"id": breach_id},
                )

    async def resolve_breach(
        self, breach_id: str, timestamp: datetime, reason: str
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE headcount_breach_events
                        SET status = 'resolved',
                            alert_status = 'resolved',
                            resolved_at = :timestamp,
                            resolution_reason = :reason
                        WHERE id = CAST(:id AS uuid) AND status = 'open'
                        """
                    ),
                    {"id": breach_id, "timestamp": timestamp, "reason": reason},
                )

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
                            id, camera_id, zone_id, count, rolling_avg, timestamp
                        )
                        VALUES (
                            CAST(:id AS uuid), CAST(:camera_id AS uuid),
                            CAST(:zone_id AS uuid), :count, :rolling_avg,
                            :timestamp
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": str(
                            snapshot_id(
                                zone_id, timestamp, config.SNAPSHOT_INTERVAL_SECONDS
                            )
                        ),
                        "camera_id": camera_id,
                        "zone_id": zone_id,
                        "count": count,
                        "rolling_avg": rolling_avg,
                        "timestamp": timestamp,
                    },
                )
