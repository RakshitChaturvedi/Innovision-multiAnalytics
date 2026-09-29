from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from shared.schemas.common import BoundingBox, TrackResult
from shared.schemas.events import DetectionEvent, FrameProvider, ZoneEvent

from services.event_processing.src.workers.zone_monitor.processor import (
    ZoneProcessor,
)
from services.event_processing.src.workers.zone_monitor.track_state_store import (
    ActiveTrackStore,
)

CAMERA = UUID("11111111-1111-1111-1111-111111111111")
ZONE = UUID("22222222-2222-2222-2222-222222222222")
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SQUARE = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]


class FakeZoneStore:
    def __init__(self, zones):
        self.zones = zones

    async def get_zones(self, camera_id):
        return self.zones


class RecordingWriter:
    """Stands in for DB insert + XADD; dedupes on event_id like ON CONFLICT."""

    def __init__(self):
        self.rows: dict[UUID, ZoneEvent] = {}
        self.published: list[ZoneEvent] = []
        self.fail_next = False

    async def write(self, ev):
        self.rows.setdefault(ev.event_id, ev)
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("xadd failed")
        self.published.append(ev)

    def types(self):
        return [e.event_type.value for e in self.published]


def make_zone(dwell=3, polygon=SQUARE):
    return {
        "id": str(ZONE), "name": "z", "type": "restricted",
        "polygon": polygon, "max_headcount": None,
        "dwell_threshold_seconds": dwell,
    }


def detection(seq, at, tracks=(), camera=CAMERA):
    """tracks: iterable of (track_id, x, y) centroid positions."""
    return DetectionEvent(
        camera_id=camera,
        frame_event_id=uuid4(),
        timestamp=T0 + timedelta(seconds=at),
        frame_reference=f"frame:{camera}:{seq}",
        frame_provider=FrameProvider.REDIS,
        frame_seq=seq,
        frame_shape=(720, 1280),
        tracks=[
            TrackResult(
                track_id=tid,
                bbox=BoundingBox(x1=x - .01, y1=y - .01, x2=x + .01, y2=y + .01),
                confidence=0.9,
                class_label="person",
            )
            for tid, x, y in tracks
        ],
        inference_latency_ms=1.0,
    )


@pytest.fixture
def writer():
    return RecordingWriter()


@pytest.fixture
def make_processor(redis_client, writer):
    def _make(zones=None):
        return ZoneProcessor(
            FakeZoneStore(zones if zones is not None else [make_zone()]),
            ActiveTrackStore(redis_client),
            writer,
        )
    return _make
