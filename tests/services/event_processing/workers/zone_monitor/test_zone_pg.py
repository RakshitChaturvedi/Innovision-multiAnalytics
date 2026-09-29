"""REAL PostgreSQL (skipped if unreachable) + real Redis: the SQL that the fakes cannot check."""
import json
from uuid import UUID

from sqlalchemy import text

from services.event_processing.src.workers.zone_monitor.event_writer import (
    ZoneEventWriter,
)
from services.event_processing.src.workers.zone_monitor.processor import (
    ZoneProcessor,
)
from services.event_processing.src.workers.zone_monitor.track_state_store import (
    ActiveTrackStore,
)
from services.event_processing.src.workers.zone_monitor.zone_store import ZoneStore

from .conftest import CAMERA, ZONE, detection

SQUARE = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]]


async def insert_zone(factory, zone_id, polygon, camera=CAMERA, dwell=3):
    async with factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO zones (id, camera_id, name, type, polygon, dwell_threshold_seconds) "
                "VALUES (CAST(:id AS uuid), CAST(:c AS uuid), 'z', 'restricted', "
                "CAST(:p AS jsonb), :d)"
            ),
            {"id": str(zone_id), "c": str(camera), "p": json.dumps(polygon), "d": dwell},
        )


async def count_rows(factory, table="zone_events"):
    async with factory() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


async def test_pipeline_against_real_pg_is_idempotent(
    pg_session_factory, redis_client
):
    await insert_zone(pg_session_factory, ZONE, SQUARE)
    store = ZoneStore(session_factory=pg_session_factory)
    store._redis = redis_client
    proc = ZoneProcessor(
        store,
        ActiveTrackStore(redis_client),
        ZoneEventWriter(pg_session_factory, redis_client),
    )

    ev = detection(1, 0, [(5, 0.5, 0.5)])
    await proc.handle(ev)
    await proc.handle(detection(2, 1, []))
    await proc.handle(detection(3, 3.5, []))  # lost -> EXITED

    assert await count_rows(pg_session_factory) == 2

    # Replaying the first message: same deterministic id, ON CONFLICT DO NOTHING.
    await ZoneEventWriter(pg_session_factory, redis_client).write(
        proc._plan(ev, str(CAMERA), await store.get_zones(str(CAMERA)), {}).events[0]
    )
    assert await count_rows(pg_session_factory) == 2

    async with pg_session_factory() as s:
        rows = (
            await s.execute(
                text("SELECT event_type, frame_reference, dwell_duration_seconds "
                     "FROM zone_events ORDER BY timestamp")
            )
        ).all()
    assert [r.event_type for r in rows] == ["entered", "exited"]
    assert rows[0].frame_reference == f"frame:{CAMERA}:1"
    assert rows[1].dwell_duration_seconds == 0

    # both events were also published, in order
    assert await redis_client.xlen("events:zone") >= 2


async def test_zone_store_loads_from_real_pg_and_skips_invalid(
    pg_session_factory, redis_client
):
    good, bad = UUID(int=1), UUID(int=2)
    await insert_zone(pg_session_factory, good, [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}])
    await insert_zone(pg_session_factory, bad, [[0, 0], [2, 0], [1, 1]])

    store = ZoneStore(session_factory=pg_session_factory)
    store._redis = redis_client
    zones = await store.get_zones(str(CAMERA))

    assert [z["id"] for z in zones] == [str(good)]
    assert zones[0]["polygon"] == [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]
    assert zones[0]["dwell_threshold_seconds"] == 3
