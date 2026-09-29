import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from shared.schemas.enums import EventType
from shared.schemas.events import DetectionEvent, ZoneEvent

from ...policies.zone_policy import (
    detect_transitions,
    get_centroid,
    point_in_polygon,
)
from .config import config
from .event_writer import ZoneEventWriter, zone_event_id
from .track_state_store import ActiveTrackStore, TrackState
from .zone_store import ZoneStore

logger = logging.getLogger(__name__)


@dataclass
class _Plan:
    events: list[ZoneEvent]
    upserts: dict[int, TrackState]
    removals: list[int]


def _exit_events(
    camera_id: str,
    track_id: int,
    st: TrackState,
    *,
    timestamp: datetime,
    frame_seq: int,
    frame_reference: str,
) -> list[ZoneEvent]:
    """EXITED for every zone a lost track was in; dwell ends at last_seen."""

    events = []

    for zone_id in sorted(st.zones):
        entry = st.entry_times.get(zone_id)
        dwell = (
            (st.last_seen - entry).total_seconds() if entry else None
        )
        events.append(
            _make_event(
                camera_id, track_id, zone_id, EventType.EXITED,
                frame_seq, timestamp, dwell, frame_reference,
            )
        )

    return events


def _make_event(
    camera_id, track_id, zone_id, event_type, frame_seq,
    timestamp, dwell, frame_reference,
) -> ZoneEvent:
    return ZoneEvent(
        event_id=zone_event_id(
            camera_id, track_id, zone_id, event_type, frame_seq
        ),
        camera_id=camera_id,
        zone_id=zone_id,
        frame_seq=frame_seq,
        track_id=track_id,
        timestamp=timestamp,
        event_type=event_type,
        dwell_duration_seconds=dwell,
        frame_reference=frame_reference,
    )


class ZoneProcessor:
    """
    Zone transition logic. All business time is the detection event
    timestamp; the wall clock is only used by sweep() to find stale cameras.

    Write order per event: DB insert + XADD for each ZoneEvent, THEN the
    track state. A failure anywhere raises before the state moves, so the
    retried message recomputes the same events with the same ids.
    """

    def __init__(
        self,
        zone_store: ZoneStore,
        state: ActiveTrackStore,
        writer: ZoneEventWriter,
    ):
        self._zones = zone_store
        self._state = state
        self._writer = writer
        self._locks: dict[str, asyncio.Lock] = {}
        self.metrics: Counter = Counter()

    def _lock(self, camera_id: str) -> asyncio.Lock:
        return self._locks.setdefault(camera_id, asyncio.Lock())

    async def handle(self, ev: DetectionEvent) -> None:
        camera_id = str(ev.camera_id)

        async with self._lock(camera_id):
            zones = await self._zones.get_zones(camera_id)
            tracks = await self._state.load(camera_id)

            if not zones and not tracks:
                self.metrics["skipped_no_zones"] += 1
                logger.debug("zone_monitor_no_zones camera=%s", camera_id)
                return

            plan = self._plan(ev, camera_id, zones, tracks)

            for zone_event in plan.events:
                await self._writer.write(zone_event)
                self.metrics[f"zone_event_{zone_event.event_type.value}"] += 1

            await self._state.save(
                camera_id,
                plan.upserts,
                plan.removals,
                wall_ts=time.time(),
            )
            self.metrics["detections_processed"] += 1

    def _plan(
        self,
        ev: DetectionEvent,
        camera_id: str,
        zones: list[dict],
        tracks: dict[int, TrackState],
    ) -> _Plan:
        now = ev.timestamp
        zones_by_id = {z["id"]: z for z in zones}

        events: list[ZoneEvent] = []
        upserts: dict[int, TrackState] = {}
        removals: list[int] = []

        seen = {t.track_id for t in ev.tracks}

        for track in ev.tracks:
            # Detection publishes only persons today; stay safe if that changes.
            if track.class_label != "person":
                continue

            centroid = get_centroid(track.bbox)
            current = {
                z["id"] for z in zones
                if point_in_polygon(centroid, z["polygon"])
            }

            prev_state = tracks.get(track.track_id)
            # Zones deleted from config can no longer be exited: forget them.
            previous = {
                z for z in (prev_state.zones if prev_state else set())
                if z in zones_by_id
            }
            entry_times = dict(prev_state.entry_times) if prev_state else {}
            notified = set(prev_state.dwell_notified) if prev_state else set()

            transitions = detect_transitions(
                previous_zones=previous,
                current_zones=current,
                zones_by_id=zones_by_id,
                entry_times=entry_times,
                now=now,
                dwell_notified=notified,
            )

            for tr in transitions:
                events.append(
                    _make_event(
                        camera_id, track.track_id, tr.zone_id,
                        tr.event_type, ev.frame_seq, now,
                        tr.dwell_duration_seconds, ev.frame_reference,
                    )
                )

                if tr.event_type == EventType.ENTERED:
                    entry_times[tr.zone_id] = now
                elif tr.event_type == EventType.EXITED:
                    entry_times.pop(tr.zone_id, None)
                    notified.discard(tr.zone_id)
                elif tr.event_type == EventType.DWELL:
                    notified.add(tr.zone_id)

            if current:
                upserts[track.track_id] = TrackState(
                    last_seen=now,
                    frame_seq=ev.frame_seq,
                    frame_reference=ev.frame_reference,
                    zones=current,
                    entry_times={z: entry_times[z] for z in current if z in entry_times},
                    dwell_notified={z for z in notified if z in current},
                )
            elif track.track_id in tracks:
                removals.append(track.track_id)

        # Tracks that vanished: exit them once the timeout has passed.
        for track_id, st in tracks.items():
            if track_id in seen:
                continue
            if (now - st.last_seen).total_seconds() <= config.LOST_TRACK_TIMEOUT_S:
                continue

            events.extend(
                _exit_events(
                    camera_id, track_id, st,
                    timestamp=now,
                    frame_seq=ev.frame_seq,
                    frame_reference=ev.frame_reference,
                )
            )
            removals.append(track_id)

        return _Plan(events, upserts, removals)

    async def sweep(self, wall_now: float | None = None) -> int:
        """
        Exit every track of cameras that stopped sending events (wall
        clock). EXITED is stamped with the track's last event time and
        last frame_seq, so a rerun after a crash yields the same ids.
        Returns the number of ZoneEvents emitted.
        """

        wall_now = time.time() if wall_now is None else wall_now
        emitted = 0

        for camera_id in await self._state.stale_cameras(
            wall_now, config.STALE_CAMERA_S
        ):
            async with self._lock(camera_id):
                tracks = await self._state.load(camera_id)

                for track_id, st in tracks.items():
                    for zone_event in _exit_events(
                        camera_id, track_id, st,
                        timestamp=st.last_seen,
                        frame_seq=st.frame_seq,
                        frame_reference=st.frame_reference,
                    ):
                        await self._writer.write(zone_event)
                        emitted += 1

                if tracks:
                    await self._state.save(camera_id, {}, list(tracks))
                await self._state.clear_meta(camera_id)

            self.metrics["stale_cameras_swept"] += 1
            logger.warning(
                "zone_monitor_stale_camera_swept camera=%s tracks=%d",
                camera_id, len(tracks),
            )

        self.metrics["sweep_exits"] += emitted
        return emitted

    async def run_sweeper(self) -> None:
        while True:
            await asyncio.sleep(config.SWEEP_INTERVAL_S)
            await self.sweep()
