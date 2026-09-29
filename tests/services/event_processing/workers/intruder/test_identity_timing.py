"""Recency-based identity ownership + grace period, on REAL Postgres + REAL
Redis. Recognition rows are written as a real recognition run would have
written them (fake model upstream); time is event time."""
from services.event_processing.src.workers.intruder.config import config
from services.event_processing.src.workers.intruder.processor import IntruderProcessor
from services.event_processing.src.workers.intruder.repository import IntruderRepository
from shared.alerting.publisher import AlertPublisher
from shared.platform_contracts.alert_event import AlertEvent
from shared.schemas.enums import EventType

from .conftest import alerts, settle, zone_event


def test_defaults():
    assert config.RECOGNITION_RECENT_S == 5.0
    assert config.INTRUDER_GRACE_S == 4.0


async def test_a_track_id_switch_alerts_once_for_the_switched_track_not_the_authorized_person(
    db, make_processor, redis_client,
):
    """The real-model timeline: track 2 = P at 0.0-1.3 s, then (id switch)
    a visitor at 2.6 s; track 3 = authorized P from 5.3 s, enters at 6.0 s,
    38 enrolled rows. Before: track 2 'owned' P at 6.0 s, so track 3 failed
    closed (false alarm) and was never revisited."""
    await db.zone()
    p = await db.person(authorized=True)
    visitor = await db.person()
    proc, _ = make_processor()

    for i in range(14):  # 0.0 .. 1.3
        await db.recognition("enrolled", person=p, at=i / 10, track=2)
    await db.recognition("visitor", person=visitor, at=2.6, sim=0.62, track=2)
    await proc.handle(zone_event(track=2, at=2.6, seq=26))

    track3 = [round(5.3 + i / 10, 1) for i in range(38)]  # 5.3 .. 9.0
    for t in [t for t in track3 if t <= 6.0]:
        await db.recognition("enrolled", person=p, at=t, track=3)
    await proc.handle(zone_event(track=3, at=6.0, seq=60))

    assert await alerts(redis_client) == []  # track 2 is in its grace period

    for t in [t for t in track3 if t > 6.0]:
        await db.recognition("enrolled", person=p, at=t, track=3)
    for n, t in enumerate((7.0, 8.0, 9.0)):
        await proc.handle(zone_event(EventType.DWELL, track=3, at=t, seq=70 + n))

    await settle(proc)  # track 2's camera side went quiet: the sweeper decides it
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["track_id"] == 2
    assert alert["metadata"]["classification_reason"] == "visitor_in_restricted"
    AlertEvent.model_validate(alert)
    reasons = {r.classification_reason for r in await db.intruder_events()}
    assert reasons == {"visitor_in_restricted"}  # track 3 never even became a candidate


async def test_b_authorized_person_alone_never_alerts(db, make_processor, redis_client):
    await db.zone()
    p = await db.person(authorized=True)
    for i in range(5):
        await db.recognition("enrolled", person=p, at=-i / 2, track=7)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7, at=0))
    await proc.handle(zone_event(EventType.DWELL, track=7, at=5, seq=50))
    await settle(proc)

    assert await alerts(redis_client) == []
    assert await db.intruder_events() == []


async def test_b_one_visitor_row_among_five_enrolled_never_alerts(db, make_processor, redis_client):
    await db.zone()
    p = await db.person(authorized=True)
    for at in (-2.5, -2.0, -1.5, -0.5, 0.0):
        await db.recognition("enrolled", person=p, at=at, track=7)
    await db.recognition("visitor", person=p, at=-1.0, sim=0.62, track=7)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7, at=0))
    await settle(proc)

    assert await alerts(redis_client) == []
    assert await db.intruder_events() == []


async def test_c_unknown_person_alerts_after_the_grace_period_not_before(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown", at=0)
    proc, _ = make_processor()

    await proc.handle(zone_event(at=0))
    await db.recognition("unknown", at=3)
    await proc.handle(zone_event(EventType.DWELL, at=3.9, seq=39))  # event time: 3.9 < 4
    assert await settle(proc, after_s=3.0) == 0  # wall clock: 3.5 s < 4 s
    assert await alerts(redis_client) == []
    (row,) = await db.intruder_events()
    assert row.alert_status == "pending" and row.alert_published is False

    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["classification_reason"] == "unknown_in_restricted"
    assert alert["timestamp"].startswith("2026-01-01T12:00:04")  # decided at the event-time deadline


async def test_c_later_zone_event_past_the_deadline_alerts_without_the_sweeper(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown", at=0)
    proc, _ = make_processor()

    await proc.handle(zone_event(at=0))
    await proc.handle(zone_event(EventType.DWELL, at=4.0, seq=40))
    await proc.handle(zone_event(EventType.DWELL, at=6.0, seq=60))
    await settle(proc)

    assert len(await alerts(redis_client)) == 1


async def test_d_blocklisted_alerts_immediately(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(blocklisted=True)
    await db.recognition("enrolled", person=person, at=0)
    proc, _ = make_processor()

    await proc.handle(zone_event(at=0))  # no sweep, no later event

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["classification_reason"] == "blocklisted"
    await settle(proc)
    assert len(await alerts(redis_client)) == 1


async def test_e_restart_during_the_grace_period_still_alerts_exactly_once(
    db, pg_session_factory, redis_client, sleeps,
):
    await db.zone()
    await db.recognition("unknown", at=0)

    def new_service():
        return IntruderProcessor(
            IntruderRepository(pg_session_factory), AlertPublisher(redis_client), sleep=sleeps
        )

    entered = zone_event(at=0)
    await new_service().handle(entered)  # ... then kill -9 during the grace period
    assert await alerts(redis_client) == []

    restarted = new_service()
    await restarted.handle(entered)  # unacked message redelivered after the restart
    await settle(restarted)
    await settle(restarted)
    await settle(new_service())  # and another restart

    got = await alerts(redis_client)
    assert len(got) == 1
    (row,) = await db.intruder_events()
    assert str(row.alert_id) == got[0]["alert_id"]


async def test_candidate_cleared_when_the_authorized_face_shows_during_grace(db, make_processor, redis_client):
    """Face turned away at entry (unknown), then recognized as authorized."""
    await db.zone()
    p = await db.person(authorized=True)
    await db.recognition("unknown", at=0)
    proc, _ = make_processor()

    await proc.handle(zone_event(at=0))
    for at in (1.0, 1.5, 2.0):
        await db.recognition("enrolled", person=p, at=at)
    await proc.handle(zone_event(EventType.DWELL, at=2.0, seq=20))
    await settle(proc)

    assert await alerts(redis_client) == []
    (row,) = await db.intruder_events()
    assert row.alert_status == "resolved"
    assert row.classification_reason == "cleared_in_grace"


async def test_exit_during_grace_fails_closed_once(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown", at=0)
    proc, _ = make_processor()

    await proc.handle(zone_event(at=0))
    await proc.handle(zone_event(EventType.EXITED, at=1.0, seq=10))
    await settle(proc)

    assert len(await alerts(redis_client)) == 1
    (row,) = await db.intruder_events()
    assert row.alert_status == "resolved"


async def test_consumer_sweeper_runs_and_stops_quietly(db, pg_url, redis_client, monkeypatch, caplog):
    """Real consumer (real Redis + Postgres): the supervised sweeper alerts a
    candidate whose grace period passed, into the TEST Redis database only,
    and stop() logs no ERROR."""
    import asyncio
    import logging
    from datetime import datetime, timedelta, timezone

    from services.event_processing.src.workers.intruder import consumer as ic
    from tests.redis_target import TEST_REDIS_DB, point_services_at_test_redis

    point_services_at_test_redis(monkeypatch)
    monkeypatch.setattr(ic.config, "DATABASE_URL", pg_url)
    monkeypatch.setattr(ic.config, "INTRUDER_SWEEP_INTERVAL_S", 0.05)
    caplog.set_level(logging.DEBUG)

    await db.zone()
    await db.recognition("unknown", at=0)
    c = ic.IntruderConsumer()
    runner = asyncio.create_task(c.start())
    for _ in range(100):
        if c._processor is not None:
            break
        await asyncio.sleep(0.02)
    assert c._publisher.connection_pool.connection_kwargs["db"] == TEST_REDIS_DB
    # The candidate was created 10 s ago on the wall clock.
    c._processor._wall_clock = lambda: datetime.now(timezone.utc) - timedelta(seconds=10)
    await c._processor.handle(zone_event(at=0))

    for _ in range(100):
        if await redis_client.xlen("alerts:live"):
            break
        await asyncio.sleep(0.05)
    assert len(await alerts(redis_client)) == 1  # in the test database, flushed after

    await c.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
