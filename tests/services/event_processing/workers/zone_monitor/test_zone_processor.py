"""Run against REAL Redis (skipped if unreachable); DB/XADD are a recording fake."""
import time

import pytest

from services.event_processing.src.workers.zone_monitor.config import config
from services.event_processing.src.workers.zone_monitor.event_writer import (
    zone_event_id,
)

from .conftest import CAMERA, T0, ZONE, detection

INSIDE = (5, 0.5, 0.5)
OUTSIDE = (5, 0.1, 0.1)


async def test_enter_dwell_once_exit(make_processor, writer):
    p = make_processor()

    await p.handle(detection(1, 0, [INSIDE]))
    for i, t in enumerate([1, 2, 3, 4, 5, 6], start=2):
        await p.handle(detection(i, t, [INSIDE]))
    await p.handle(detection(9, 7, [OUTSIDE]))

    assert writer.types() == ["entered", "dwell", "exited"]
    dwell, exited = writer.published[1], writer.published[2]
    assert dwell.dwell_duration_seconds == 3  # first frame at/after threshold
    assert exited.dwell_duration_seconds == 7
    # event time, not wall clock
    assert exited.timestamp == T0.replace(second=7)


async def test_track_disappears_exits_after_timeout(make_processor, writer):
    p = make_processor()
    await p.handle(detection(1, 0, [INSIDE]))

    # within the timeout: nothing yet
    await p.handle(detection(2, config.LOST_TRACK_TIMEOUT_S - 0.5))
    assert writer.types() == ["entered"]

    await p.handle(detection(3, config.LOST_TRACK_TIMEOUT_S + 0.5))
    assert writer.types() == ["entered", "exited"]
    assert writer.published[1].dwell_duration_seconds == 0  # ends at last_seen

    # state removed: no second EXITED
    await p.handle(detection(4, 10))
    assert writer.types() == ["entered", "exited"]


async def test_stale_camera_swept(make_processor, writer):
    p = make_processor()
    await p.handle(detection(1, 0, [INSIDE]))
    await p.handle(detection(2, 1, [INSIDE]))

    # camera alive (wall clock): nothing
    assert await p.sweep(wall_now=time.time() + 1) == 0

    assert await p.sweep(wall_now=time.time() + config.STALE_CAMERA_S + 1) == 1
    assert writer.types() == ["entered", "exited"]
    exited = writer.published[-1]
    assert exited.timestamp == T0.replace(second=1)  # last event time
    assert exited.dwell_duration_seconds == 1
    assert exited.frame_reference == f"frame:{CAMERA}:2"

    # idempotent: a second sweep emits nothing
    assert await p.sweep(wall_now=time.time() + 100) == 0


async def test_retry_creates_no_duplicates(make_processor, writer):
    p = make_processor()
    ev = detection(1, 0, [INSIDE])
    writer.fail_next = True  # XADD fails after the insert

    with pytest.raises(ConnectionError):
        await p.handle(ev)
    assert len(writer.rows) == 1  # row inserted, state not moved

    await p.handle(ev)  # redelivery of the same message
    assert len(writer.rows) == 1  # same deterministic id, no duplicate row
    assert writer.types() == ["entered"]
    assert writer.published[0].event_id == zone_event_id(
        CAMERA, 5, ZONE, "entered", 1
    )
    assert writer.published[0].frame_reference == f"frame:{CAMERA}:1"


async def test_redelivery_after_success_emits_nothing(make_processor, writer):
    p = make_processor()
    ev = detection(1, 0, [INSIDE])
    await p.handle(ev)
    await p.handle(ev)  # crash between state write and XACK
    assert writer.types() == ["entered"]


async def test_uses_event_time_not_wall_clock(make_processor, writer):
    p = make_processor()
    await p.handle(detection(1, 0, [INSIDE]))
    assert writer.published[0].timestamp == T0
