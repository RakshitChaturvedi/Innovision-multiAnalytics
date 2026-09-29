"""INTRUDER_ALERT_COOLDOWN_S. REAL PostgreSQL + REAL Redis (skip if unreachable)."""
from uuid import UUID

import pytest

from services.event_processing.src.workers.intruder.config import IntruderConfig
from services.event_processing.src.workers.intruder.processor import IntruderProcessor
from services.event_processing.src.workers.intruder.repository import IntruderRepository
from shared.alerting.publisher import AlertPublisher

from .conftest import alerts, zone_event

ZONE_B = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def processor(pg_session_factory, redis_client, sleeps):
    def _make(cooldown_s):
        return IntruderProcessor(
            IntruderRepository(pg_session_factory), AlertPublisher(redis_client),
            sleep=sleeps, cooldown_s=cooldown_s,
        )
    return _make


async def rows(db):
    return await db.rows(
        "SELECT track_id, zone_id, alert_published, alert_suppressed, alert_id "
        "FROM intruder_events ORDER BY first_detected_at, track_id"
    )


async def test_cooldown_disabled_publishes_every_intrusion(db, processor, redis_client):
    await db.zone()
    proc = processor(0)
    await proc.handle(zone_event(track=1, at=0, seq=1))
    await proc.handle(zone_event(track=2, at=5, seq=2))

    assert [a["metadata"]["track_id"] for a in await alerts(redis_client)] == [1, 2]
    assert [(r.alert_published, r.alert_suppressed) for r in await rows(db)] == [
        (True, False), (True, False)
    ]


async def test_second_alert_within_cooldown_is_recorded_not_published(db, processor, redis_client):
    await db.zone()
    proc = processor(60)
    await proc.handle(zone_event(track=1, at=0, seq=1))
    await proc.handle(zone_event(track=2, at=30, seq=2))   # inside 60 s
    await proc.handle(zone_event(track=3, at=61, seq=3))   # 61 s after the published one

    assert [a["metadata"]["track_id"] for a in await alerts(redis_client)] == [1, 3]
    r1, r2, r3 = await rows(db)
    assert (r1.alert_published, r1.alert_suppressed) == (True, False)
    assert (r2.alert_published, r2.alert_suppressed, r2.alert_id) == (False, True, None)
    assert (r3.alert_published, r3.alert_suppressed) == (True, False)
    assert proc.metrics["alerts_suppressed_cooldown"] == 1


async def test_suppressed_event_is_never_published_on_redelivery_or_dwell(db, processor, redis_client):
    await db.zone()
    proc = processor(60)
    await proc.handle(zone_event(track=1, at=0, seq=1))
    ev = zone_event(track=2, at=10, seq=2)
    await proc.handle(ev)
    await proc.handle(ev)                                   # redelivery
    await proc.handle(zone_event(track=2, at=200, seq=9))   # later DWELL/ENTERED, same open row

    assert len(await alerts(redis_client)) == 1
    assert [r.alert_suppressed for r in await rows(db)] == [False, True]


async def test_cooldown_is_per_camera_and_zone(db, processor, redis_client):
    await db.zone()
    await db.zone(zone_id=ZONE_B, name="Lab")
    proc = processor(60)
    await proc.handle(zone_event(track=1, at=0, seq=1))
    await proc.handle(zone_event(track=2, at=5, seq=2, zone=ZONE_B))

    assert len(await alerts(redis_client)) == 2
    assert not any(r.alert_suppressed for r in await rows(db))


async def test_suppressed_row_does_not_extend_the_cooldown(db, processor, redis_client):
    """Only a PUBLISHED alert opens a window: t=0 published, t=50 suppressed,
    t=70 is >60 s after the published one and alerts."""
    await db.zone()
    proc = processor(60)
    await proc.handle(zone_event(track=1, at=0, seq=1))
    await proc.handle(zone_event(track=2, at=50, seq=2))
    await proc.handle(zone_event(track=3, at=70, seq=3))
    assert [a["metadata"]["track_id"] for a in await alerts(redis_client)] == [1, 3]


def test_setting_default_and_env(monkeypatch):
    monkeypatch.delenv("INTRUDER_ALERT_COOLDOWN_S", raising=False)
    assert IntruderConfig(_env_file=None).ALERT_COOLDOWN_S == 0
    monkeypatch.setenv("INTRUDER_ALERT_COOLDOWN_S", "45")
    assert IntruderConfig(_env_file=None).ALERT_COOLDOWN_S == 45
