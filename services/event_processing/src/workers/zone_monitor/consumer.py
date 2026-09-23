import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.events import (
    DetectionEvent,
    ZoneEvent,
)
from shared.schemas.enums import EventType

from ...policies.zone_policy import (
    detect_transitions,
    get_centroid,
    point_in_polygon,
)

from .config import config
from .track_state_store import TrackStateStore
from .zone_store import ZoneStore


logger = logging.getLogger(__name__)


class ZoneMonitorConsumer(BaseStreamConsumer):

    def __init__(self):

        super().__init__(
            stream_key=config.DETECTIONS_STREAM,
            group_name=config.CONSUMER_GROUP,
            consumer_name=config.CONSUMER_NAME,
        )

        self._engine = create_async_engine(
            config.DATABASE_URL
        )

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

        self._zone_store: ZoneStore | None = None
        self._track_state: TrackStateStore | None = None
        self._publisher: aioredis.Redis | None = None

    async def start(self):

        redis_client = await aioredis.from_url(
            f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}"
        )

        self._zone_store = ZoneStore()

        await self._zone_store.initialize(
            redis_client
        )

        self._track_state = TrackStateStore(
            redis_client
        )

        self._publisher = redis_client

        logger.info(
            "zone_monitor_consumer_ready"
        )

        await super().start()

    async def process(
        self,
        msg_id: str,
        data: dict,
    ) -> None:

        raw = (
            data.get(b"data")
            or data.get("data")
        )

        if isinstance(raw, bytes):
            raw = raw.decode()

        detection_event = (
            DetectionEvent.model_validate_json(raw)
        )

        camera_id = str(
            detection_event.camera_id
        )

        now = datetime.now(timezone.utc)

        zones = await self._zone_store.get_zones(
            camera_id
        )

        if not zones:
            return

        zones_by_id = {
            zone["id"]: zone
            for zone in zones
        }

        for track in detection_event.tracks:

            # Detection currently publishes person detections,
            # but keeping this check makes Zone Monitor safe.
            if track.class_label != "person":
                continue

            centroid = get_centroid(
                track.bbox
            )

            current_zones: set[str] = set()

            for zone in zones:

                if point_in_polygon(
                    centroid,
                    zone["polygon"],
                ):
                    current_zones.add(
                        zone["id"]
                    )

            previous_zones = (
                await self._track_state.get_current_zones(
                    camera_id,
                    track.track_id,
                )
            )

            entry_times = {}

            for zone_id in (
                current_zones | previous_zones
            ):

                entry_time = (
                    await self._track_state.get_entry_time(
                        camera_id,
                        track.track_id,
                        zone_id,
                    )
                )

                if entry_time:
                    entry_times[zone_id] = (
                        entry_time
                    )

            transitions = detect_transitions(
                previous_zones=previous_zones,
                current_zones=current_zones,
                zones_by_id=zones_by_id,
                entry_times=entry_times,
                now=now,
            )

            for transition in transitions:

                await self._write_and_publish(
                    detection_event=detection_event,
                    track_id=track.track_id,
                    transition=transition,
                    now=now,
                )

                if (
                    transition.event_type
                    == EventType.ENTERED
                ):

                    await self._track_state.set_entry_time(
                        camera_id,
                        track.track_id,
                        transition.zone_id,
                        now,
                    )

                elif (
                    transition.event_type
                    == EventType.EXITED
                ):

                    await self._track_state.delete_entry_time(
                        camera_id,
                        track.track_id,
                        transition.zone_id,
                    )

            await self._track_state.set_current_zones(
                camera_id,
                track.track_id,
                current_zones,
            )

    async def _write_and_publish(
        self,
        detection_event: DetectionEvent,
        track_id: int,
        transition,
        now: datetime,
    ) -> None:

        event_id = (
            detection_event.event_id
        )

        zone_event = ZoneEvent(
            camera_id=detection_event.camera_id,
            zone_id=transition.zone_id,
            frame_seq=detection_event.frame_seq,
            track_id=track_id,
            timestamp=now,
            event_type=transition.event_type,
            dwell_duration_seconds=(
                transition.dwell_duration_seconds
            ),
        )

        async with self._session_factory() as session:

            async with session.begin():

                await session.execute(
                    text(
                        """
                        INSERT INTO zone_events (
                            id,
                            camera_id,
                            zone_id,
                            frame_seq,
                            track_id,
                            timestamp,
                            event_type,
                            dwell_duration_seconds
                        )
                        VALUES (
                            CAST(:id AS uuid),
                            CAST(:camera_id AS uuid),
                            CAST(:zone_id AS uuid),
                            :frame_seq,
                            :track_id,
                            :timestamp,
                            :event_type,
                            :dwell_duration_seconds
                        )
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "id": str(
                            zone_event.event_id
                        ),
                        "camera_id": str(
                            zone_event.camera_id
                        ),
                        "zone_id": str(
                            zone_event.zone_id
                        ),
                        "frame_seq": (
                            zone_event.frame_seq
                        ),
                        "track_id": (
                            zone_event.track_id
                        ),
                        "timestamp": (
                            zone_event.timestamp
                        ),
                        "event_type": (
                            zone_event.event_type.value
                        ),
                        "dwell_duration_seconds": (
                            zone_event.dwell_duration_seconds
                        ),
                    },
                )

        await self._publisher.xadd(
            config.ZONE_EVENTS_STREAM,
            {
                "data": (
                    zone_event.model_dump_json()
                )
            },
            maxlen=config.ZONE_EVENTS_MAXLEN,
            approximate=True,
        )

        logger.info(
            "zone_event camera=%s track=%s "
            "zone=%s event=%s",
            detection_event.camera_id,
            track_id,
            transition.zone_id,
            transition.event_type.value,
        )