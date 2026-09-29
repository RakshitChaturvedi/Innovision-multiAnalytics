"""REAL PostgreSQL + REAL Redis (each test skips if its service is unreachable)."""
import asyncio

import pytest

from services.event_processing.src.workers.intruder.config import config
from services.event_processing.src.workers.intruder.consumer import IntruderConsumer
from services.event_processing.src.workers.intruder.processor import IntruderProcessor
from services.event_processing.src.workers.intruder.repository import IntruderRepository
from shared.alerting.publisher import AlertPublisher
from shared.errors import PermanentError
from shared.platform_contracts.alert_event import AlertEvent
from shared.schemas.enums import EventType

from .conftest import T0, alerts, settle, zone_event


async def test_unknown_person_alerts(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown", at=-1)
    proc, _ = make_processor()

    await proc.handle(zone_event())
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["alert_type"] == "intruder"
    assert alert["severity"] == "critical"
    assert alert["source_uc"] == "uc1"
    assert alert["frame_provider"] == "minio"
    AlertEvent.model_validate(alert)  # platform contract accepts it

    (row,) = await db.intruder_events()
    assert row.classification_reason == "unknown_in_restricted"
    assert row.alert_published is True
    assert str(row.alert_id) == alert["alert_id"]


async def test_no_recognition_fails_closed(db, make_processor, redis_client, sleeps):
    await db.zone()
    proc, _ = make_processor()

    await proc.handle(zone_event())
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["alert_type"] == "intruder"
    assert alert["severity"] == "high"
    assert alert["metadata"]["classification_reason"] == "unidentified_in_restricted"
    # 5 attempts, 0.3 s between them
    assert sleeps.calls == [config.RECOGNITION_LOOKUP_DELAY_SECONDS] * 4
    assert config.RECOGNITION_LOOKUP_DELAY_SECONDS == 0.3


async def test_no_recognition_outside_restricted_zone_does_not_alert(db, make_processor, redis_client, sleeps):
    await db.zone(type_="monitored")
    proc, _ = make_processor()
    await proc.handle(zone_event())
    assert await alerts(redis_client) == []
    assert sleeps.calls == []  # no waiting where fail-closed doesn't apply


async def test_authorized_enrolled_never_alerts(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    await db.recognition("enrolled", person=person, at=-2)
    proc, _ = make_processor()

    await proc.handle(zone_event())
    await settle(proc)

    assert await alerts(redis_client) == []
    assert await db.intruder_events() == []


async def test_authorized_majority_wins_over_better_unknown_row(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    for at in (-4, -3, -2):
        await db.recognition("enrolled", person=person, at=at, sim=0.8)
    await db.recognition("unknown", at=-1, sim=0.99)  # face turned away, better score
    proc, _ = make_processor()

    await proc.handle(zone_event())
    await settle(proc)

    assert await alerts(redis_client) == []


async def test_single_authorized_row_without_majority_fails_closed(db, make_processor, redis_client):
    """Old rule: any one authorized row authorized the track."""
    await db.zone()
    person = await db.person(authorized=True)
    await db.recognition("enrolled", person=person, at=-3, sim=0.8)
    await db.recognition("unknown", at=-2, sim=0.5)
    await db.recognition("unknown", at=-1, sim=0.5)
    proc, _ = make_processor()

    await proc.handle(zone_event())
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["classification_reason"] == "unidentified_in_restricted"


async def test_dominant_track_keeps_identity_stray_track_alerts(db, make_processor, redis_client):
    """(a) The authorized person's own track (38 rows) keeps the identity; an
    unknown person's track with 2 stray rows of that person and no face of
    its own is unverified and alerts. Old rule: both failed closed."""
    await db.zone()
    person = await db.person(authorized=True)
    for i in range(38):
        await db.recognition("enrolled", person=person, at=-0.5 * i, track=7)
    for at in (-3, -1):
        await db.recognition("enrolled", person=person, at=at, track=8)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7))
    await proc.handle(zone_event(track=8, seq=11))
    assert proc.metrics["identity_conflict"] == 1  # at the zone events
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["track_id"] == 8
    assert alert["metadata"]["classification_reason"] == "unidentified_in_restricted"


@pytest.mark.parametrize("counts", [(5, 4), (4, 4), (6, 4)])
async def test_same_person_on_two_tracks_without_clear_winner_fails_closed_for_both(
    db, make_processor, redis_client, counts
):
    """(b) 5 vs 4 (and a tie, and 6 vs 4 < 2x): no track keeps the identity."""
    await db.zone()
    person = await db.person(authorized=True)
    for track, n in zip((7, 8), counts):
        for i in range(n):
            await db.recognition("enrolled", person=person, at=-0.5 * i, track=track)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7))
    await proc.handle(zone_event(track=8, seq=11))
    assert proc.metrics["identity_conflict"] == 2  # at the zone events
    await settle(proc)

    got = await alerts(redis_client)
    assert sorted(a["metadata"]["track_id"] for a in got) == [7, 8]
    assert {a["metadata"]["classification_reason"] for a in got} == {"unidentified_in_restricted"}


async def test_exactly_twice_the_runner_up_keeps_identity(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    for i in range(4):
        await db.recognition("enrolled", person=person, at=-0.5 * i, track=7)
    for i in range(2):
        await db.recognition("enrolled", person=person, at=-0.5 * i, track=8)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7))
    await proc.handle(zone_event(track=8, seq=11))
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["track_id"] == 8


async def test_losing_rows_still_count_against_the_majority(db, make_processor, redis_client):
    """Track 8 loses 2 rows of P to track 7; its single row of authorized Q
    must not become a majority by dropping them."""
    await db.zone()
    p = await db.person(authorized=True)
    q = await db.person(authorized=True)
    for i in range(10):
        await db.recognition("enrolled", person=p, at=-0.5 * i, track=7)
    for at in (-3, -2):
        await db.recognition("enrolled", person=p, at=at, track=8)
    await db.recognition("enrolled", person=q, at=-1, track=8)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=8, seq=11))
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["classification_reason"] == "unidentified_in_restricted"


async def test_blocklist_wins_on_dominant_track(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True, blocklisted=True)
    for i in range(10):
        await db.recognition("enrolled", person=person, at=-0.5 * i, track=7)
    await db.recognition("enrolled", person=person, at=-1, track=8)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7))
    await proc.handle(zone_event(track=8, seq=11))

    got = await alerts(redis_client)
    assert {a["metadata"]["classification_reason"] for a in got} == {"blocklisted"}
    assert sorted(a["metadata"]["track_id"] for a in got) == [7, 8]


async def test_authorized_person_alone_never_alerts(db, make_processor, redis_client):
    """(c)"""
    await db.zone()
    person = await db.person(authorized=True)
    for i in range(5):
        await db.recognition("enrolled", person=person, at=-0.5 * i, track=7)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7))
    await settle(proc)

    assert await alerts(redis_client) == []
    assert proc.metrics["identity_conflict"] == 0


async def test_authorized_person_alone_with_one_visitor_row_never_alerts(db, make_processor, redis_client):
    """(d) five enrolled rows + one visitor row (face briefly below threshold)."""
    await db.zone()
    person = await db.person(authorized=True)
    for i in range(5):
        await db.recognition("enrolled", person=person, at=-0.5 * i, track=7)
    await db.recognition("visitor", person=person, at=-3, sim=0.62, track=7)
    proc, _ = make_processor()

    await proc.handle(zone_event(track=7))
    await settle(proc)

    assert await alerts(redis_client) == []


async def test_same_person_on_other_track_outside_window_is_no_conflict(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    await db.recognition("enrolled", person=person, at=-1, track=7)
    await db.recognition("enrolled", person=person, at=-120, track=8)  # long gone
    await db.recognition("enrolled", person=person, at=-1, track=9, sim=0.9)
    proc, _ = make_processor()
    # other camera, same person, same time: not a conflict either
    await db.run(
        "UPDATE recognition_events SET camera_id = CAST(:c AS uuid) WHERE track_id = 9",
        c="33333333-3333-3333-3333-333333333333",
    )

    await proc.handle(zone_event(track=7))
    await settle(proc)

    assert await alerts(redis_client) == []


async def test_blocklisted_wins_even_if_authorized(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True, blocklisted=True)
    await db.recognition("enrolled", person=person)
    proc, _ = make_processor()

    await proc.handle(zone_event())

    (alert,) = await alerts(redis_client)
    assert alert["severity"] == "critical"
    assert alert["metadata"]["classification_reason"] == "blocklisted"
    assert alert["alert_type"] == "intruder"


async def test_enrolled_unauthorized_is_restricted_entry(db, make_processor, redis_client):
    await db.zone()
    person = await db.person()
    await db.recognition("enrolled", person=person)
    proc, _ = make_processor()

    await proc.handle(zone_event())
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["alert_type"] == "restricted_entry"
    assert alert["severity"] == "high"


async def test_visitor_is_restricted_entry(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("visitor")
    proc, _ = make_processor()
    await proc.handle(zone_event())
    await settle(proc)
    (alert,) = await alerts(redis_client)
    assert alert["alert_type"] == "restricted_entry"


async def test_publish_fails_once_then_retry_yields_exactly_one_alert(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown")
    proc, publisher = make_processor(fail_times=1)

    await proc.handle(zone_event())
    with pytest.raises(ConnectionError):
        await settle(proc)

    assert await alerts(redis_client) == []
    (row,) = await db.intruder_events()
    assert row.alert_published is False  # row exists, alert still owed

    await settle(proc)  # next sweeper pass

    (alert,) = await alerts(redis_client)
    (row,) = await db.intruder_events()
    assert row.alert_published is True
    assert str(row.alert_id) == alert["alert_id"]
    assert publisher.attempts == 2


async def test_publish_failure_on_the_stream_is_retried_by_redelivery(db, make_processor, redis_client):
    """Blocklisted alerts go out inside process(): a failed publish raises and
    the redelivered message publishes it."""
    await db.zone()
    person = await db.person(blocklisted=True)
    await db.recognition("enrolled", person=person)
    proc, publisher = make_processor(fail_times=1)
    ev = zone_event()

    with pytest.raises(ConnectionError):
        await proc.handle(ev)
    await proc.handle(ev)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["classification_reason"] == "blocklisted"
    assert publisher.attempts == 2


async def test_crash_after_publish_republishes_same_alert_id(db, make_processor, redis_client, monkeypatch):
    await db.zone()
    await db.recognition("unknown")
    proc, _ = make_processor()

    real = proc._repo.mark_published
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db down")
        return await real(*a, **k)

    monkeypatch.setattr(proc._repo, "mark_published", flaky)

    await proc.handle(zone_event())
    with pytest.raises(ConnectionError):
        await settle(proc)
    await settle(proc)

    sent = await alerts(redis_client)
    assert len(sent) == 2  # at-least-once...
    assert sent[0]["alert_id"] == sent[1]["alert_id"]  # ...but the platform dedupes on alert_id
    assert len(await db.intruder_events()) == 1


async def test_dwell_and_redelivery_do_not_duplicate(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown")
    proc, _ = make_processor()

    await proc.handle(zone_event(EventType.ENTERED, at=0))
    await proc.handle(zone_event(EventType.ENTERED, at=0))  # redelivered
    await proc.handle(zone_event(EventType.DWELL, at=5, seq=20))

    assert len(await alerts(redis_client)) == 1
    assert len(await db.intruder_events()) == 1


async def test_concurrent_duplicates_create_one_row_and_one_alert_id(db, pg_session_factory, redis_client, sleeps):
    await db.zone()
    await db.recognition("unknown")

    def make():
        return IntruderProcessor(
            IntruderRepository(pg_session_factory), AlertPublisher(redis_client), sleep=sleeps
        )

    ev = zone_event()
    await asyncio.gather(make().handle(ev), make().handle(ev))
    await settle(make())

    assert len(await db.intruder_events()) == 1
    assert len({a["alert_id"] for a in await alerts(redis_client)}) == 1


async def test_exit_resolves_at_event_time_and_new_intrusion_alerts_again(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown", at=0)
    proc, _ = make_processor()

    await proc.handle(zone_event(EventType.ENTERED, at=0))
    await proc.handle(zone_event(EventType.EXITED, at=4, seq=30))

    (row,) = await db.intruder_events()
    assert row.alert_status == "resolved"
    assert row.resolved_at == T0.replace(second=4)

    # same track re-enters: a fresh intrusion, a fresh alert
    await db.recognition("unknown", at=9)
    await proc.handle(zone_event(EventType.ENTERED, at=10, seq=40))
    await settle(proc)
    assert len(await db.intruder_events()) == 2
    assert len({a["alert_id"] for a in await alerts(redis_client)}) == 2


async def test_late_redelivery_of_a_resolved_zone_event_is_ignored(db, make_processor, redis_client):
    await db.zone()
    await db.recognition("unknown")
    proc, _ = make_processor()
    entered = zone_event(EventType.ENTERED, at=0)

    await proc.handle(entered)
    await proc.handle(zone_event(EventType.EXITED, at=4, seq=30))
    await proc.handle(entered)  # old message reclaimed after the row was resolved

    (row,) = await db.intruder_events()
    assert row.alert_status == "resolved"
    assert len(await alerts(redis_client)) == 1


async def test_exit_without_open_event_is_a_noop(db, make_processor):
    await db.zone()
    proc, _ = make_processor()
    await proc.handle(zone_event(EventType.EXITED))
    assert await db.intruder_events() == []


async def test_recognition_outside_time_window_is_ignored(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    # An authorized match, but 60 s old and 10 s in the future: both ignored.
    await db.recognition("enrolled", person=person, at=-60)
    await db.recognition("enrolled", person=person, at=10)
    proc, _ = make_processor()

    await proc.handle(zone_event(at=0))
    await settle(proc)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["classification_reason"] == "unidentified_in_restricted"


async def test_recognition_at_window_edges_counts(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    await db.recognition("enrolled", person=person, at=-30)  # exactly MAX_AGE
    proc, _ = make_processor()
    await proc.handle(zone_event(at=0))
    await settle(proc)
    assert await alerts(redis_client) == []


async def test_recognition_for_other_track_is_ignored(db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    await db.recognition("enrolled", person=person, track=99)
    proc, _ = make_processor()
    await proc.handle(zone_event(track=7))
    await settle(proc)
    assert len(await alerts(redis_client)) == 1


async def test_unknown_zone_is_permanent(make_processor, db):
    proc, _ = make_processor()
    with pytest.raises(PermanentError):
        await proc.handle(zone_event())


async def test_consumer_rejects_bad_payloads_as_permanent():
    consumer = IntruderConsumer()
    consumer._processor = object()
    try:
        with pytest.raises(PermanentError):
            await consumer.process("1-0", {b"other": b"x"}, "events:zone")
        with pytest.raises(PermanentError):
            await consumer.process("1-0", {b"data": b"{not json"}, "events:zone")
    finally:
        await consumer._engine.dispose()
