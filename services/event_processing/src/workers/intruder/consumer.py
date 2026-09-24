import asyncio
import logging
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.enums import (
    AlertStatus,
    AlertType,
    EventType,
)
from shared.schemas.events import (
    AlertEvent,
    RecognitionEvent,
    ZoneEvent,
)

from ...policies.intruder_policy import (
    classify_intruder,
)

from .config import config


logger = logging.getLogger(__name__)


class IntruderConsumer(BaseStreamConsumer):

    def __init__(self):

        super().__init__(
            stream_key=config.ZONE_EVENTS_STREAM,
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

        self._publisher: aioredis.Redis | None = None

    async def start(self):

        self._publisher = await aioredis.from_url(
            f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}"
        )

        logger.info(
            "intruder_consumer_ready"
        )

        await super().start()

    async def stop(self):

        await super().stop()

        await self._engine.dispose()

    async def process(
        self,
        msg_id: str,
        data: dict,
    ) -> None:

        raw = (
            data.get(b"data")
            or data.get("data")
        )

        if raw is None:
            logger.error(
                "zone_event_missing_data msg_id=%s",
                msg_id,
            )
            return

        if isinstance(raw, bytes):
            raw = raw.decode()

        zone_event = ZoneEvent.model_validate_json(
            raw
        )

        # We only classify entry events.
        if zone_event.event_type == EventType.ENTERED:

            await self._handle_entry(
                zone_event
            )

        # Exit means an existing intruder event
        # should be resolved.
        elif zone_event.event_type == EventType.EXITED:

            await self._handle_exit(
                zone_event
            )

    async def _handle_entry(
        self,
        zone_event: ZoneEvent,
    ) -> None:

        zone = await self._fetch_zone(
            str(zone_event.zone_id)
        )

        if zone is None:
            logger.warning(
                "intruder_zone_not_found zone=%s",
                zone_event.zone_id,
            )
            return

        recognition = await self._fetch_latest_recognition(
            camera_id=str(zone_event.camera_id),
            track_id=zone_event.track_id,
        )

        if recognition is None:

            logger.warning(
                "intruder_recognition_not_found "
                "camera=%s track=%s zone=%s",
                zone_event.camera_id,
                zone_event.track_id,
                zone_event.zone_id,
            )

            return

        person_id = recognition.get(
            "person_id"
        )

        identity_tag = recognition[
            "identity_tag"
        ]

        similarity_score = float(
            recognition.get(
                "similarity_score",
                0.0,
            )
        )

        is_blocklisted = await self._is_blocklisted(
            person_id
        )

        authorized_person_ids = (
            await self._fetch_authorized_person_ids(
                str(zone_event.zone_id)
            )
        )

        classification = classify_intruder(
            identity_tag=identity_tag,
            person_id=person_id,
            zone_type=zone["type"],
            authorized_person_ids=authorized_person_ids,
            is_blocklisted=is_blocklisted,
            similarity_score=similarity_score,
        )

        # Authorized person or non-restricted zone.
        if classification is None:
            return

        # Prevent duplicate alerts for the same
        # track while the intrusion is still open.
        existing_event = await self._get_open_intruder_event(
            camera_id=str(zone_event.camera_id),
            zone_id=str(zone_event.zone_id),
            track_id=zone_event.track_id,
        )

        if existing_event:

            await self._update_existing_intruder_event(
                event_id=existing_event["id"],
                timestamp=zone_event.timestamp,
            )

            return

        intruder_event_id = uuid.uuid4()

        await self._create_intruder_event(
            event_id=intruder_event_id,
            zone_event=zone_event,
            person_id=person_id,
            classification_reason=classification.reason,
            timestamp=zone_event.timestamp,
        )

        alert = self._build_alert(
            zone_event=zone_event,
            zone_name=zone["name"],
            person_id=person_id,
            classification_reason=classification.reason,
            severity=classification.severity,
            recognition=recognition,
            intruder_event_id=intruder_event_id,
        )

        await self._publish_alert(
            alert
        )

        logger.warning(
            "intruder_detected camera=%s "
            "zone=%s track=%s reason=%s",
            zone_event.camera_id,
            zone_event.zone_id,
            zone_event.track_id,
            classification.reason,
        )

    async def _handle_exit(
        self,
        zone_event: ZoneEvent,
    ) -> None:

        existing_event = await self._get_open_intruder_event(
            camera_id=str(zone_event.camera_id),
            zone_id=str(zone_event.zone_id),
            track_id=zone_event.track_id,
        )

        if existing_event is None:
            return

        await self._resolve_intruder_event(
            event_id=existing_event["id"],
            timestamp=zone_event.timestamp,
        )

        logger.info(
            "intruder_resolved camera=%s "
            "zone=%s track=%s",
            zone_event.camera_id,
            zone_event.zone_id,
            zone_event.track_id,
        )

    async def _fetch_zone(
        self,
        zone_id: str,
    ) -> dict | None:

        async with self._session_factory() as session:

            result = await session.execute(
                text(
                    """
                    SELECT
                        id,
                        name,
                        type
                    FROM zones
                    WHERE id = CAST(:zone_id AS uuid)
                    """
                ),
                {
                    "zone_id": zone_id
                },
            )

            row = result.fetchone()

            if row is None:
                return None

            return {
                "id": str(row.id),
                "name": row.name,
                "type": row.type,
            }

    async def _fetch_authorized_person_ids(
        self,
        zone_id: str,
    ) -> list[str]:

        async with self._session_factory() as session:

            result = await session.execute(
                text(
                    """
                    SELECT person_id
                    FROM zone_authorized_persons
                    WHERE zone_id = CAST(:zone_id AS uuid)
                    """
                ),
                {
                    "zone_id": zone_id
                },
            )

            return [
                str(row.person_id)
                for row in result.fetchall()
            ]

    async def _fetch_latest_recognition(
        self,
        camera_id: str,
        track_id: int,
    ) -> dict | None:

        # Recognition and zone processing run independently.
        # Retry briefly in case the zone event arrives first.
        for attempt in range(
            config.RECOGNITION_LOOKUP_RETRIES
        ):

            async with self._session_factory() as session:

                result = await session.execute(
                    text(
                        """
                        SELECT
                            id,
                            person_id,
                            identity_tag,
                            similarity_score,
                            timestamp
                        FROM recognition_events
                        WHERE camera_id = CAST(:camera_id AS uuid)
                          AND track_id = :track_id
                        ORDER BY timestamp DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "camera_id": camera_id,
                        "track_id": track_id,
                    },
                )

                row = result.fetchone()

                if row is not None:

                    return {
                        "id": str(row.id),
                        "person_id": (
                            str(row.person_id)
                            if row.person_id
                            else None
                        ),
                        "identity_tag": row.identity_tag,
                        "similarity_score": (
                            row.similarity_score
                        ),
                        "timestamp": row.timestamp,
                    }

            if attempt + 1 < config.RECOGNITION_LOOKUP_RETRIES:
                await asyncio.sleep(
                    config.RECOGNITION_LOOKUP_DELAY_SECONDS
                )

        return None

    async def _is_blocklisted(
        self,
        person_id: str | None,
    ) -> bool:

        if person_id is None:
            return False

        async with self._session_factory() as session:

            result = await session.execute(
                text(
                    """
                    SELECT 1
                    FROM blocklist
                    WHERE person_id = CAST(:person_id AS uuid)
                      AND (
                          expires_at IS NULL
                          OR expires_at > now()
                      )
                    LIMIT 1
                    """
                ),
                {
                    "person_id": person_id
                },
            )

            return result.fetchone() is not None

    async def _get_open_intruder_event(
        self,
        camera_id: str,
        zone_id: str,
        track_id: int,
    ) -> dict | None:

        async with self._session_factory() as session:

            result = await session.execute(
                text(
                    """
                    SELECT
                        id,
                        first_detected_at,
                        last_seen_at
                    FROM intruder_events
                    WHERE camera_id = CAST(:camera_id AS uuid)
                      AND zone_id = CAST(:zone_id AS uuid)
                      AND track_id = :track_id
                      AND alert_status != 'resolved'
                    ORDER BY first_detected_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "camera_id": camera_id,
                    "zone_id": zone_id,
                    "track_id": track_id,
                },
            )

            row = result.fetchone()

            if row is None:
                return None

            return {
                "id": str(row.id),
                "first_detected_at": row.first_detected_at,
                "last_seen_at": row.last_seen_at,
            }

    async def _create_intruder_event(
        self,
        event_id: uuid.UUID,
        zone_event: ZoneEvent,
        person_id: str | None,
        classification_reason: str,
        timestamp: datetime,
    ) -> None:

        async with self._session_factory() as session:

            async with session.begin():

                await session.execute(
                    text(
                        """
                        INSERT INTO intruder_events (
                            id,
                            zone_id,
                            camera_id,
                            track_id,
                            person_id,
                            classification_reason,
                            first_detected_at,
                            last_seen_at,
                            alert_status
                        )
                        VALUES (
                            CAST(:id AS uuid),
                            CAST(:zone_id AS uuid),
                            CAST(:camera_id AS uuid),
                            :track_id,
                            CAST(:person_id AS uuid),
                            :classification_reason,
                            :timestamp,
                            :timestamp,
                            'pending'
                        )
                        """
                    ),
                    {
                        "id": str(event_id),
                        "zone_id": str(
                            zone_event.zone_id
                        ),
                        "camera_id": str(
                            zone_event.camera_id
                        ),
                        "track_id": zone_event.track_id,
                        "person_id": person_id,
                        "classification_reason": (
                            classification_reason
                        ),
                        "timestamp": timestamp,
                    },
                )

    async def _update_existing_intruder_event(
        self,
        event_id: str,
        timestamp: datetime,
    ) -> None:

        async with self._session_factory() as session:

            async with session.begin():

                await session.execute(
                    text(
                        """
                        UPDATE intruder_events
                        SET last_seen_at = :timestamp
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {
                        "id": event_id,
                        "timestamp": timestamp,
                    },
                )

    async def _resolve_intruder_event(
        self,
        event_id: str,
        timestamp: datetime,
    ) -> None:

        async with self._session_factory() as session:

            async with session.begin():

                await session.execute(
                    text(
                        """
                        UPDATE intruder_events
                        SET
                            last_seen_at = :timestamp,
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

    def _build_alert(
        self,
        zone_event: ZoneEvent,
        zone_name: str,
        person_id: str | None,
        classification_reason: str,
        severity,
        recognition: dict,
        intruder_event_id: uuid.UUID,
    ) -> AlertEvent:

        title, description = self._build_alert_content(
            zone_name=zone_name,
            classification_reason=classification_reason,
            person_id=person_id,
        )

        return AlertEvent(
            camera_id=zone_event.camera_id,
            timestamp=zone_event.timestamp,
            severity=severity,
            alert_type=AlertType.INTRUDER,
            title=title,
            description=description,
            source_event_ids=[
                zone_event.event_id,
                intruder_event_id,
            ],
            # frame_reference=recognition.get( 
            #     "frame_reference"
            # ),
            frame_reference=None, # Note this is not correct, it needs to be changed after frame_ref is corrected
            status=AlertStatus.PENDING,
            metadata={
                "zone_id": str(
                    zone_event.zone_id
                ),
                "track_id": zone_event.track_id,
                "person_id": person_id,
                "classification_reason": (
                    classification_reason
                ),
                "similarity_score": recognition.get(
                    "similarity_score",
                    0.0,
                ),
            },
        )

    @staticmethod
    def _build_alert_content(
        zone_name: str,
        classification_reason: str,
        person_id: str | None,
    ) -> tuple[str, str]:

        if classification_reason == "blocklisted":

            return (
                f"Blocklisted Individual Detected — {zone_name}",
                "A blocklisted individual has been "
                f"detected in zone '{zone_name}'.",
            )

        if classification_reason == "enrolled_unauthorized":

            return (
                f"Unauthorized Access Attempt — {zone_name}",
                "An enrolled individual without "
                f"authorization entered zone '{zone_name}'.",
            )

        if classification_reason == "unknown_in_restricted":

            return (
                f"Unknown Individual in Restricted Zone — {zone_name}",
                "An unrecognized individual has entered "
                f"restricted zone '{zone_name}'.",
            )

        if classification_reason == "visitor_in_restricted":

            return (
                f"Unverified Individual in Restricted Zone — {zone_name}",
                "An individual with uncertain identity has "
                f"entered restricted zone '{zone_name}'.",
            )

        return (
            f"Intruder Detected — {zone_name}",
            f"An unauthorized individual has been detected "
            f"in zone '{zone_name}'.",
        )

    async def _publish_alert(
        self,
        alert: AlertEvent,
    ) -> None:

        if self._publisher is None:
            raise RuntimeError(
                "Intruder publisher is not initialized"
            )

        await self._publisher.xadd(
            config.ALERTS_STREAM,
            {
                "data": alert.model_dump_json()
            },
            maxlen=config.ALERTS_MAXLEN,
            approximate=True,
        )