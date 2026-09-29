import asyncio
import logging
import uuid
from collections import Counter
from datetime import timedelta

from shared.alerting.publisher import build_platform_alert
from shared.errors import PermanentError
from shared.schemas.enums import EventType
from shared.schemas.events import ZoneEvent

from ...policies.intruder_policy import (
    alert_type_for,
    decide,
    severity_for,
)
from .config import config
from .repository import IntruderRepository

logger = logging.getLogger(__name__)

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "innovision.intruder_events")


def intruder_event_id_for(zone_event: ZoneEvent) -> uuid.UUID:
    """Deterministic: redelivery of the same zone event maps to one row/alert."""

    return uuid.uuid5(_NAMESPACE, str(zone_event.event_id))


class IntruderProcessor:
    """
    ENTERED/DWELL -> classify and (once) alert. EXITED -> resolve.

    Alert creation is idempotent and safe to retry:
      1. INSERT ... ON CONFLICT DO NOTHING, else load the open row
      2. if alert_published is false: publish (deterministic alert_id)
      3. mark alert_published = true
    A failure at any step raises; the redelivered message resumes at the
    first step that did not complete. A crash between 2 and 3 republishes the
    same alert_id, which the platform dedupes.
    """

    def __init__(
        self,
        repo: IntruderRepository,
        publisher,
        sleep=asyncio.sleep,
    ):
        self._repo = repo
        self._publisher = publisher
        self._sleep = sleep
        self.metrics: Counter = Counter()

    async def handle(self, zone_event: ZoneEvent) -> None:
        if zone_event.event_type == EventType.EXITED:
            await self._handle_exit(zone_event)
        elif zone_event.event_type in (EventType.ENTERED, EventType.DWELL):
            await self._handle_entry(zone_event)

    async def _handle_exit(self, ev: ZoneEvent) -> None:
        resolved = await self._repo.resolve_open(
            str(ev.camera_id), str(ev.zone_id), ev.track_id, ev.timestamp
        )

        if resolved:
            self.metrics["resolved"] += 1
            logger.info(
                "intruder_resolved camera=%s zone=%s track=%s",
                ev.camera_id, ev.zone_id, ev.track_id,
            )

    async def _handle_entry(self, ev: ZoneEvent) -> None:
        camera_id, zone_id = str(ev.camera_id), str(ev.zone_id)

        zone = await self._repo.fetch_zone(zone_id)

        if zone is None:
            self.metrics["unknown_zone"] += 1
            raise PermanentError(f"zone {zone_id} not found")

        open_event = await self._repo.get_open(camera_id, zone_id, ev.track_id)

        if open_event is not None:
            # Already classified: no lookup, no sleeping. Publish only if a
            # previous attempt died before the alert went out.
            await self._repo.touch(open_event["id"], ev.timestamp)

            if not open_event["alert_published"]:
                await self._publish(ev, zone, open_event)

            return

        rows = await self._lookup_recognitions(ev, zone)
        authorized = await self._repo.authorized_person_ids(zone_id)
        blocklisted = await self._repo.blocklisted_person_ids(
            [r["person_id"] for r in rows if r["person_id"]], ev.timestamp
        )

        conflicted = await self._conflicting_identities(ev, zone, rows)

        classification, chosen = decide(
            rows, zone["type"], authorized, blocklisted, conflicted
        )

        if classification is None:
            self.metrics["not_an_intruder"] += 1
            return

        row = await self._repo.create_or_get_open(
            event_id=intruder_event_id_for(ev),
            camera_id=camera_id,
            zone_id=zone_id,
            track_id=ev.track_id,
            person_id=chosen["person_id"] if chosen else None,
            reason=classification.reason,
            timestamp=ev.timestamp,
        )

        if row is None:
            # This very zone event was handled and resolved before.
            self.metrics["already_handled"] += 1
            return

        if row["alert_published"]:
            return

        await self._publish(
            ev, zone, row,
            similarity=chosen["similarity_score"] if chosen else None,
        )

        logger.warning(
            "intruder_detected camera=%s zone=%s track=%s reason=%s",
            camera_id, zone_id, ev.track_id, classification.reason,
        )

    async def _conflicting_identities(
        self, ev: ZoneEvent, zone: dict, rows: list[dict]
    ) -> set[str]:
        """
        Enrolled persons of this track that are ALSO matched on another
        track of the same camera in the same window. One person cannot be
        two tracks at once, so at least one of those matches is wrong; in a
        restricted zone both tracks are then treated as unverified.
        """

        if zone["type"] != "restricted":
            return set()

        person_ids = sorted({
            r["person_id"] for r in rows
            if r["identity_tag"] == "enrolled" and r["person_id"]
        })
        if not person_ids:
            return set()

        conflicted = await self._repo.persons_on_other_tracks(
            str(ev.camera_id),
            ev.track_id,
            person_ids,
            ev.timestamp - timedelta(seconds=config.RECOGNITION_MAX_AGE_S),
            ev.timestamp + timedelta(seconds=config.RECOGNITION_FUTURE_S),
        )
        if conflicted:
            self.metrics["identity_conflict"] += 1
            logger.warning(
                "intruder_identity_conflict camera=%s track=%s zone=%s persons=%s",
                ev.camera_id, ev.track_id, ev.zone_id, sorted(conflicted),
            )
        return conflicted

    async def _lookup_recognitions(self, ev: ZoneEvent, zone: dict) -> list[dict]:
        """
        Rows for (camera, track) in [ts - MAX_AGE, ts + FUTURE] (event time).
        Recognition may lag the zone event, so retry while there is nothing;
        only restricted zones are worth waiting for (fail-closed follows).
        """

        start = ev.timestamp - timedelta(seconds=config.RECOGNITION_MAX_AGE_S)
        end = ev.timestamp + timedelta(seconds=config.RECOGNITION_FUTURE_S)
        attempts = (
            config.RECOGNITION_LOOKUP_RETRIES
            if zone["type"] == "restricted"
            else 1
        )

        for attempt in range(attempts):
            rows = await self._repo.recognitions_in_window(
                str(ev.camera_id), ev.track_id, start, end
            )

            if rows:
                return rows

            if attempt + 1 < attempts:
                await self._sleep(config.RECOGNITION_LOOKUP_DELAY_SECONDS)

        self.metrics["no_recognition"] += 1
        logger.warning(
            "intruder_recognition_not_found camera=%s track=%s zone=%s",
            ev.camera_id, ev.track_id, ev.zone_id,
        )
        return []

    async def _publish(
        self,
        ev: ZoneEvent,
        zone: dict,
        row: dict,
        similarity: float | None = None,
    ) -> None:
        reason = row["classification_reason"]
        intruder_event_id = uuid.UUID(row["id"])

        title, description = self._build_alert_content(
            zone_name=zone["name"],
            classification_reason=reason,
            person_id=row["person_id"],
        )

        alert = build_platform_alert(
            domain_event_id=intruder_event_id,
            camera_id=ev.camera_id,
            timestamp=ev.timestamp,
            severity=severity_for(reason),
            alert_type=alert_type_for(reason),
            title=title,
            description=description,
            frame_seq=ev.frame_seq,
            metadata={
                "source_event_ids": [ev.event_id, intruder_event_id],
                "zone_id": str(ev.zone_id),
                "track_id": ev.track_id,
                "person_id": row["person_id"],
                "classification_reason": reason,
                "similarity_score": similarity,
            },
        )

        await self._publisher.publish(alert)
        await self._repo.mark_published(row["id"], alert.alert_id)
        self.metrics["alerts_published"] += 1

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

        if classification_reason == "unidentified_in_restricted":

            return (
                f"Unidentified Individual in Restricted Zone — {zone_name}",
                "A person entered restricted zone "
                f"'{zone_name}' and could not be identified "
                "(no face recognition result).",
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
