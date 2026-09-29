import asyncio
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from shared.alerting.publisher import build_platform_alert
from shared.errors import PermanentError
from shared.schemas.enums import EventType
from shared.schemas.events import ZoneEvent

from ...policies.intruder_policy import (
    alert_type_for,
    decide,
    resolve_ownership,
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
    ENTERED/DWELL -> classify; EXITED -> resolve.

    Restricted zone without an authorized winner: the entry becomes a
    CANDIDATE row (awaiting_decision) instead of an alert. It is
    re-evaluated with the newest recognition rows on every later zone event
    of that track and by the wall-clock sweeper; it alerts only if still
    unresolved at decide_at = entry + INTRUDER_GRACE_S (event time), or
    when the track exits first (fail-closed). An authorized winner in the
    meantime clears it. Blocklisted persons alert immediately. The
    candidate lives in Postgres, so it survives a restart.

    Alert creation is idempotent and safe to retry:
      1. INSERT ... ON CONFLICT DO NOTHING, else load the open row
      2. candidate: finalize_candidate claims it atomically (one winner)
      3. if alert_published is false: publish (deterministic alert_id)
      4. mark alert_published = true
    With INTRUDER_ALERT_COOLDOWN_S > 0 the cooldown is checked at PUBLISH
    time (step 3), i.e. only once the grace period decided to alert; a
    candidate still waiting is never suppressed. A row first detected
    within the cooldown of another published alert on the same camera+zone
    is marked alert_suppressed and never published (also not by a retry).
    A failure at any step raises; the redelivered message (or the sweeper)
    resumes at the first step that did not complete. A crash between 3 and
    4 republishes the same alert_id, which the platform dedupes.
    """

    def __init__(
        self,
        repo: IntruderRepository,
        publisher,
        sleep=asyncio.sleep,
        cooldown_s: float | None = None,
        wall_clock=lambda: datetime.now(timezone.utc),
    ):
        self._repo = repo
        self._cooldown_s = (
            config.ALERT_COOLDOWN_S if cooldown_s is None else cooldown_s
        )
        self._publisher = publisher
        self._sleep = sleep
        # Wall clock only stamps candidate_since for the sweeper; every
        # decision uses event time.
        self._wall_clock = wall_clock
        self.metrics: Counter = Counter()

    async def handle(self, zone_event: ZoneEvent) -> None:
        if zone_event.event_type == EventType.EXITED:
            await self._handle_exit(zone_event)
        elif zone_event.event_type in (EventType.ENTERED, EventType.DWELL):
            await self._handle_entry(zone_event)

    async def _handle_exit(self, ev: ZoneEvent) -> None:
        camera_id, zone_id = str(ev.camera_id), str(ev.zone_id)
        open_event = await self._repo.get_open(camera_id, zone_id, ev.track_id)

        if open_event is not None:
            zone = await self._repo.fetch_zone(zone_id)
            if zone is None:
                self.metrics["unknown_zone"] += 1
                raise PermanentError(f"zone {zone_id} not found")
            # Left before the grace period ended: decide now (fail-closed).
            # An alert still owed goes out before the row is resolved.
            await self._settle(open_event, zone, at=ev.timestamp, final=True)

        resolved = await self._repo.resolve_open(
            camera_id, zone_id, ev.track_id, ev.timestamp
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
            # Already known: no lookup retries, no sleeping.
            await self._repo.touch(open_event["id"], ev.timestamp)
            await self._settle(open_event, zone, at=ev.timestamp, final=False)
            return

        rows = await self._lookup_recognitions(ev, zone)
        classification, chosen = await self._classify(
            camera_id, zone, ev.track_id, ev.timestamp, rows
        )

        if classification is None:
            self.metrics["not_an_intruder"] += 1
            return

        immediate = classification.reason == "blocklisted"
        decide_at = (
            None if immediate
            else ev.timestamp + timedelta(seconds=config.INTRUDER_GRACE_S)
        )

        row = await self._repo.create_or_get_open(
            event_id=intruder_event_id_for(ev),
            camera_id=camera_id,
            zone_id=zone_id,
            track_id=ev.track_id,
            person_id=chosen["person_id"] if chosen else None,
            reason=classification.reason,
            timestamp=ev.timestamp,
            zone_event_id=ev.event_id,
            frame_seq=ev.frame_seq,
            decide_at=decide_at,
            candidate_since=self._wall_clock(),
        )

        if row is None:
            # This very zone event was handled and resolved before.
            self.metrics["already_handled"] += 1
            return

        if row["awaiting_decision"]:
            self.metrics["candidates"] += 1
            logger.info(
                "intruder_candidate camera=%s zone=%s track=%s reason=%s decide_at=%s",
                camera_id, zone_id, ev.track_id, row["classification_reason"],
                row["decide_at"].isoformat() if row["decide_at"] else None,
            )
            return

        if row["alert_published"] or row["alert_suppressed"]:
            return

        await self._publish(
            row, zone, ev.timestamp,
            similarity=chosen["similarity_score"] if chosen else None,
        )

    async def _settle(self, row: dict, zone: dict, at, final: bool) -> None:
        """Move an open row forward at event time `at`: re-evaluate a
        candidate (clear / keep waiting / alert), or publish an owed alert.
        `final`: the grace period is over regardless of `at` (exit, sweep)."""

        if not row["awaiting_decision"]:
            if not row["alert_published"] and not row["alert_suppressed"]:
                await self._publish(row, zone, at)
            return

        rows = await self._repo.recognitions_in_window(
            row["camera_id"], row["track_id"],
            at - timedelta(seconds=config.RECOGNITION_MAX_AGE_S),
            at + timedelta(seconds=config.RECOGNITION_FUTURE_S),
        )
        classification, chosen = await self._classify(
            row["camera_id"], zone, row["track_id"], at, rows
        )

        if classification is None:
            if await self._repo.clear_candidate(row["id"], at):
                self.metrics["candidates_cleared"] += 1
                logger.info(
                    "intruder_candidate_cleared camera=%s zone=%s track=%s",
                    row["camera_id"], row["zone_id"], row["track_id"],
                )
            return

        due = (
            final
            or classification.reason == "blocklisted"
            or (row["decide_at"] is not None and at >= row["decide_at"])
        )
        if not due:
            return

        claimed = await self._repo.finalize_candidate(
            row["id"], classification.reason,
            chosen["person_id"] if chosen else None,
        )
        if claimed is None:
            return  # the stream or the sweeper already decided it

        await self._publish(
            claimed, zone, at,
            similarity=chosen["similarity_score"] if chosen else None,
        )

    async def sweep(self, now=None) -> int:
        """
        One sweeper pass (wall clock). Candidates whose grace period has
        passed without any later zone event (camera quiet, service
        restarted) are decided at their event-time deadline; alerts still
        owed after a failed publish are retried. Returns rows acted on.
        Errors propagate: the supervisor logs and restarts the sweeper.
        """

        now = now or self._wall_clock()
        cutoff = now - timedelta(seconds=config.INTRUDER_GRACE_S)
        owed = await self._repo.owed_rows(cutoff)

        for row in owed:
            zone = await self._repo.fetch_zone(row["zone_id"])
            if zone is None:
                self.metrics["unknown_zone"] += 1
                logger.warning(
                    "intruder_sweep_zone_missing zone=%s row=%s",
                    row["zone_id"], row["id"],
                )
                continue
            at = row["decide_at"] or row["last_seen_at"]
            await self._settle(row, zone, at=at, final=True)

        if owed:
            self.metrics["swept"] += len(owed)
        return len(owed)

    async def _classify(self, camera_id, zone, track_id, at, rows):
        authorized = await self._repo.authorized_person_ids(zone["id"])
        blocklisted = await self._repo.blocklisted_person_ids(
            [r["person_id"] for r in rows if r["person_id"]], at
        )
        conflicted, unverified = await self._conflicting_identities(
            camera_id, zone, track_id, at, rows
        )
        return decide(
            rows, zone["type"], authorized, blocklisted, conflicted, unverified
        )

    async def _conflicting_identities(
        self, camera_id: str, zone: dict, track_id: int, at, rows: list[dict]
    ) -> tuple[set[str], set[str]]:
        """
        (conflicted, unverified) enrolled persons of this track, decided on
        RECENT evidence only (see resolve_ownership): rows from the last
        RECOGNITION_RECENT_S before `at` on this camera. A track that
        switched to another person, or has no recent rows of a person, does
        not own that person.
        """

        if zone["type"] != "restricted":
            return set(), set()

        person_ids = sorted({
            r["person_id"] for r in rows
            if r["identity_tag"] == "enrolled" and r["person_id"]
        })
        if not person_ids:
            return set(), set()

        recent = await self._repo.camera_rows_by_track(
            camera_id,
            at - timedelta(seconds=config.RECOGNITION_RECENT_S),
            at,
        )
        conflicted, unverified = resolve_ownership(track_id, person_ids, recent)
        if conflicted or unverified:
            self.metrics["identity_conflict"] += 1
            logger.warning(
                "intruder_identity_conflict camera=%s track=%s zone=%s "
                "no_clear_winner=%s owned_by_other_track=%s",
                camera_id, track_id, zone["id"], sorted(conflicted),
                sorted(unverified),
            )
        return conflicted, unverified

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
        row: dict,
        zone: dict,
        timestamp,
        similarity: float | None = None,
    ) -> None:
        if await self._suppressed_by_cooldown(row):
            return

        reason = row["classification_reason"]
        intruder_event_id = uuid.UUID(row["id"])

        title, description = self._build_alert_content(
            zone_name=zone["name"],
            classification_reason=reason,
            person_id=row["person_id"],
        )

        source_ids = [intruder_event_id]
        if row["zone_event_id"]:
            source_ids.insert(0, uuid.UUID(row["zone_event_id"]))

        alert = build_platform_alert(
            domain_event_id=intruder_event_id,
            camera_id=uuid.UUID(row["camera_id"]),
            timestamp=timestamp,
            severity=severity_for(reason),
            alert_type=alert_type_for(reason),
            title=title,
            description=description,
            frame_seq=row["frame_seq"],
            metadata={
                "source_event_ids": source_ids,
                "zone_id": row["zone_id"],
                "track_id": row["track_id"],
                "person_id": row["person_id"],
                "classification_reason": reason,
                "similarity_score": similarity,
            },
        )

        await self._publisher.publish(alert)
        await self._repo.mark_published(row["id"], alert.alert_id)
        self.metrics["alerts_published"] += 1
        logger.warning(
            "intruder_detected camera=%s zone=%s track=%s reason=%s",
            row["camera_id"], row["zone_id"], row["track_id"], reason,
        )

    async def _suppressed_by_cooldown(self, row: dict) -> bool:
        """Window is anchored on the row's first_detected_at (event time), so
        a redelivery or a later DWELL reaches the same decision."""
        if row["alert_suppressed"]:
            self.metrics["alerts_suppressed_cooldown_redelivered"] += 1
            return True
        if self._cooldown_s <= 0:
            return False
        first = row["first_detected_at"]
        if not await self._repo.published_alert_within(
            row["camera_id"], row["zone_id"], row["id"],
            since=first - timedelta(seconds=self._cooldown_s), until=first,
        ):
            return False
        await self._repo.mark_suppressed(row["id"])
        self.metrics["alerts_suppressed_cooldown"] += 1
        logger.warning(
            "intruder_alert_suppressed_cooldown camera=%s zone=%s track=%s "
            "intruder_event=%s cooldown_s=%s",
            row["camera_id"], row["zone_id"], row["track_id"], row["id"],
            self._cooldown_s,
        )
        return True

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
