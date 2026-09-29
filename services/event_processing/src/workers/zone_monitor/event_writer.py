import logging
import uuid

import redis.asyncio as aioredis
from sqlalchemy import text

from shared.schemas.events import ZoneEvent

from .config import config

logger = logging.getLogger(__name__)

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "innovision.zone_events")


def zone_event_id(
    camera_id,
    track_id: int,
    zone_id,
    event_type,
    frame_seq: int,
) -> uuid.UUID:
    """Deterministic id: a retried message yields the same event id."""

    event_type = getattr(event_type, "value", event_type)

    return uuid.uuid5(
        _NAMESPACE,
        f"{camera_id}:{track_id}:{zone_id}:{event_type}:{frame_seq}",
    )


INSERT_SQL = text(
    """
    INSERT INTO zone_events (
        id, camera_id, zone_id, frame_seq, track_id,
        timestamp, event_type, dwell_duration_seconds,
        frame_reference
    )
    VALUES (
        CAST(:id AS uuid),
        CAST(:camera_id AS uuid),
        CAST(:zone_id AS uuid),
        :frame_seq, :track_id, :timestamp, :event_type,
        :dwell_duration_seconds, :frame_reference
    )
    ON CONFLICT DO NOTHING
    """
)


class ZoneEventWriter:
    """DB insert (idempotent), then XADD events:zone. Errors propagate."""

    def __init__(self, session_factory, redis_client: aioredis.Redis):
        self._session_factory = session_factory
        self._redis = redis_client

    async def write(self, ev: ZoneEvent) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    INSERT_SQL,
                    {
                        "id": str(ev.event_id),
                        "camera_id": str(ev.camera_id),
                        "zone_id": str(ev.zone_id),
                        "frame_seq": ev.frame_seq,
                        "track_id": ev.track_id,
                        "timestamp": ev.timestamp,
                        "event_type": ev.event_type.value,
                        "dwell_duration_seconds": (
                            ev.dwell_duration_seconds
                        ),
                        "frame_reference": ev.frame_reference,
                    },
                )

        await self._redis.xadd(
            config.ZONE_EVENTS_STREAM,
            {"data": ev.model_dump_json()},
            maxlen=config.ZONE_EVENTS_MAXLEN,
            approximate=True,
        )

        logger.info(
            "zone_event camera=%s track=%s zone=%s event=%s",
            ev.camera_id,
            ev.track_id,
            ev.zone_id,
            ev.event_type.value,
        )
