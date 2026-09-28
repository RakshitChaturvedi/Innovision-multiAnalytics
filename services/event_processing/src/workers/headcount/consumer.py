import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.events import (
    DetectionEvent,
)
from shared.alerting.publisher import AlertPublisher, build_platform_alert
from shared.schemas.enums import AlertSeverity, AlertType

from ...policies.zone_policy import (
    get_centroid,
    point_in_polygon,
)
from ...policies.headcount_policy import assess_headcount

from .config import config
from .headcount_store import HeadcountStore
from ..zone_monitor.zone_store import ZoneStore


logger = logging.getLogger(__name__)


class HeadcountConsumer(BaseStreamConsumer):

    def __init__(self):
        super().__init__(
            stream_key=config.DETECTIONS_STREAM,
            group_name=config.CONSUMER_GROUP,
            consumer_name=config.CONSUMER_NAME,
        )

        self._zone_store: ZoneStore | None = None
        self._headcount_store: HeadcountStore | None = None
        self._publisher: aioredis.Redis | None = None

    async def start(self):
        redis_client = await aioredis.from_url(
            f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}"
        )

        self._zone_store = ZoneStore()
        await self._zone_store.initialize(redis_client)

        self._headcount_store = HeadcountStore()
        await self._headcount_store.initialize(redis_client)

        self._publisher = redis_client

        logger.info("headcount_consumer_ready")

        await super().start()

    async def stop(self):
        await super().stop()
        if self._headcount_store and self._headcount_store._engine:
            await self._headcount_store._engine.dispose()

    async def process(
        self,
        msg_id: str,
        data: dict,
    ) -> None:
        raw = data.get(b"data") or data.get("data")

        if raw is None:
            logger.error(
                "headcount_missing_data msg_id=%s",
                msg_id,
            )
            return

        if isinstance(raw, bytes):
            raw = raw.decode()

        detection_event = DetectionEvent.model_validate_json(raw)
        camera_id = str(detection_event.camera_id)
        now = datetime.now(timezone.utc)

        zones = await self._zone_store.get_zones(camera_id)
        if not zones:
            return

        # Pre-calculate centroids for all person tracks
        centroids = []
        for track in detection_event.tracks:
            if track.class_label == "person":
                centroids.append(get_centroid(track.bbox))

        for zone in zones:
            max_headcount = zone.get("max_headcount")
            if max_headcount is None:
                continue

            zone_id = zone["id"]
            
            # Option A logic: Count point in polygon again (safer across consumer groups)
            count = 0
            for centroid in centroids:
                if point_in_polygon(centroid, zone["polygon"]):
                    count += 1

            rolling_avg = await self._headcount_store.update_rolling_average(
                zone_id, count, now
            )

            if await self._headcount_store.should_write_snapshot(zone_id):
                await self._headcount_store.write_snapshot(
                    camera_id=camera_id,
                    zone_id=zone_id,
                    count=count,
                    rolling_avg=rolling_avg,
                    timestamp=now,
                )

            assessment = assess_headcount(
                zone_id=zone_id,
                zone_name=zone["name"],
                count=count,
                max_headcount=max_headcount,
            )

            if assessment is None:
                continue

            open_breach = await self._headcount_store.get_open_breach(zone_id)

            if assessment.is_breach and not open_breach:
                # New breach
                breach_event_id = await self._headcount_store.write_breach_event(
                    zone_id=zone_id,
                    camera_id=camera_id,
                    count=count,
                    threshold=assessment.threshold,
                    timestamp=now,
                )

                alert = build_platform_alert(
                    domain_event_id=breach_event_id,
                    camera_id=detection_event.camera_id,
                    timestamp=now,
                    severity=AlertSeverity.HIGH,
                    alert_type=AlertType.HEADCOUNT_BREACH.value,
                    title="Headcount Threshold Exceeded",
                    description=(
                        f"Zone '{zone['name']}' reached {count} "
                        f"persons (threshold: {assessment.threshold})."
                    ),
                    frame_seq=detection_event.frame_seq,
                    metadata={
                        "source_event_ids": [
                            detection_event.event_id,
                            breach_event_id,
                        ],
                        "zone_id": zone_id,
                        "count": count,
                        "threshold": assessment.threshold,
                        "rolling_avg": round(rolling_avg, 2),
                    },
                )

                await AlertPublisher(
                    self._publisher,
                    stream=config.ALERTS_STREAM,
                    maxlen=config.ALERTS_MAXLEN,
                ).publish(alert)

                logger.warning(
                    "headcount_breach camera=%s zone=%s count=%s threshold=%s",
                    camera_id,
                    zone_id,
                    count,
                    assessment.threshold,
                )

            elif not assessment.is_breach and open_breach:
                # Resolve breach
                await self._headcount_store.resolve_breach_event(
                    event_id=open_breach["id"],
                    timestamp=now,
                )
                logger.info(
                    "headcount_breach_resolved camera=%s zone=%s",
                    camera_id,
                    zone_id,
                )
