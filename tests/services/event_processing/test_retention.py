"""Retention job. REAL PostgreSQL (skips if unreachable; see conftest)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from services.event_processing.src.retention import RULES, RetentionConfig, RetentionJob

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
OLD_RAW = NOW - timedelta(days=8)       # older than 7 days
NEW_RAW = NOW - timedelta(days=6)
OLD_EVT = NOW - timedelta(days=31)      # older than 30 days
NEW_EVT = NOW - timedelta(days=29)
VEC = "[" + ",".join(["0.1"] * 512) + "]"
CAM = str(uuid4())
ZONE = str(uuid4())

ALL = (
    "detection_events, recognition_events, face_embeddings, zone_events, "
    "headcount_snapshots, intruder_events, headcount_breach_events, enrolled_persons, zones"
)


@pytest.fixture
async def engine(pg_url):
    eng = create_async_engine(pg_url, poolclass=NullPool)
    async with eng.begin() as c:
        await c.execute(text(f"TRUNCATE {ALL} CASCADE"))
    yield eng
    await eng.dispose()


async def ex(engine, sql, **p):
    async with engine.begin() as c:
        return await c.execute(text(sql), p)


async def count(engine, table, where="true"):
    async with engine.connect() as c:
        return (await c.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))).scalar()


_seq = iter(range(1, 10**9))


async def detection(engine, ts, n=1):
    for _ in range(n):
        await ex(engine,
                 "INSERT INTO detection_events (camera_id, track_id, bounding_box, class_label, "
                 "confidence, frame_reference, frame_seq, frame_timestamp) VALUES "
                 "(CAST(:c AS uuid), 1, CAST(:b AS jsonb), 'person', 0.9, 'frame:x:1', :s, :ts)",
                 c=CAM, b=json.dumps([0, 0, 1, 1]), s=next(_seq), ts=ts)


async def embedding(engine, created, enrollment=False, person=None):
    eid = str(uuid4())
    await ex(engine,
             "INSERT INTO face_embeddings (id, person_id, embedding, quality_score, is_enrollment, "
             "created_at) VALUES (CAST(:i AS uuid), CAST(:p AS uuid), CAST(:v AS vector), 0.9, :e, :ts)",
             i=eid, p=person, v=VEC, e=enrollment, ts=created)
    return eid


async def recognition(engine, ts, emb):
    await ex(engine,
             "INSERT INTO recognition_events (camera_id, detection_event_id, track_id, "
             "similarity_score, identity_tag, embedding_id, quality_score, timestamp) VALUES "
             "(CAST(:c AS uuid), CAST(:d AS uuid), 1, 0.5, 'unknown', CAST(:e AS uuid), 0.9, :ts)",
             c=CAM, d=str(uuid4()), e=emb, ts=ts)


async def zone_event(engine, ts):
    await ex(engine,
             "INSERT INTO zone_events (id, camera_id, zone_id, frame_seq, track_id, timestamp, "
             "event_type) VALUES (CAST(:i AS uuid), CAST(:c AS uuid), CAST(:z AS uuid), 1, 1, :ts, "
             "'ENTERED')", i=str(uuid4()), c=CAM, z=ZONE, ts=ts)


async def snapshot(engine, ts):
    await ex(engine,
             "INSERT INTO headcount_snapshots (camera_id, zone_id, count, rolling_avg, timestamp) "
             "VALUES (CAST(:c AS uuid), CAST(:z AS uuid), 3, 3.0, :ts)", c=CAM, z=ZONE, ts=ts)


async def seed_protected(engine, ts):
    """Rows that must survive any age."""
    await ex(engine,
             "INSERT INTO zones (id, camera_id, name, type, polygon) VALUES "
             "(CAST(:z AS uuid), CAST(:c AS uuid), 'Z', 'restricted', CAST(:p AS jsonb))",
             z=ZONE, c=CAM, p=json.dumps([[0, 0], [1, 0], [1, 1]]))
    await ex(engine,
             "INSERT INTO intruder_events (id, zone_id, camera_id, track_id, classification_reason, "
             "first_detected_at, last_seen_at, alert_status) VALUES (CAST(:i AS uuid), "
             "CAST(:z AS uuid), CAST(:c AS uuid), 1, 'unknown_in_restricted', :ts, :ts, 'resolved')",
             i=str(uuid4()), z=ZONE, c=CAM, ts=ts)
    await ex(engine,
             "INSERT INTO headcount_breach_events (zone_id, camera_id, count, threshold, timestamp, "
             "status, alert_status) VALUES (CAST(:z AS uuid), CAST(:c AS uuid), 9, 5, :ts, "
             "'resolved', 'resolved')", z=ZONE, c=CAM, ts=ts)
    person = str(uuid4())
    await ex(engine, "INSERT INTO enrolled_persons (id, name) VALUES (CAST(:i AS uuid), 'p')", i=person)
    await embedding(engine, ts, enrollment=True, person=person)


def job(engine, batch=10_000):
    return RetentionJob(RetentionConfig(RETENTION_BATCH_SIZE=batch), engine=engine)


async def test_deletes_only_expired_rows_and_never_protected_ones(engine, caplog):
    ancient = NOW - timedelta(days=3650)
    await seed_protected(engine, ancient)
    await detection(engine, OLD_RAW, 3)
    await detection(engine, NEW_RAW, 2)
    old_emb, new_emb = await embedding(engine, OLD_RAW), await embedding(engine, NEW_RAW)
    await recognition(engine, OLD_RAW, old_emb)
    await recognition(engine, NEW_RAW, new_emb)
    for ts in (OLD_EVT, NEW_EVT, OLD_RAW):  # OLD_RAW is inside the 30-day window
        await zone_event(engine, ts)
        await snapshot(engine, ts)

    caplog.set_level(logging.INFO, logger="services.event_processing.src.retention")
    deleted = await job(engine).run_once(now=NOW)

    assert deleted == {
        "detection_events": 3, "recognition_events": 1, "face_embeddings": 1,
        "zone_events": 1, "headcount_snapshots": 1,
    }
    assert await count(engine, "detection_events") == 2
    assert await count(engine, "recognition_events") == 1
    assert await count(engine, "face_embeddings", "NOT is_enrollment") == 1
    assert await count(engine, "zone_events") == 2
    assert await count(engine, "headcount_snapshots") == 2
    # protected, even though 10 years old
    assert await count(engine, "face_embeddings", "is_enrollment") == 1
    assert await count(engine, "intruder_events") == 1
    assert await count(engine, "headcount_breach_events") == 1
    assert "retention_run deleted_total=7" in caplog.text
    assert "detection_events=3" in caplog.text

    assert sum((await job(engine).run_once(now=NOW)).values()) == 0


async def test_batches_until_done(engine):
    await detection(engine, OLD_RAW, 7)
    await detection(engine, NEW_RAW, 1)
    deleted = await job(engine, batch=2).run_once(now=NOW)
    assert deleted["detection_events"] == 7
    assert await count(engine, "detection_events") == 1


async def test_old_embedding_still_referenced_by_a_kept_recognition_is_kept(engine):
    """The embedding FK cascades into recognition_events: deleting a
    referenced embedding would drop a recognition row inside its window."""
    emb = await embedding(engine, OLD_RAW)
    await recognition(engine, NEW_RAW, emb)
    await job(engine).run_once(now=NOW)
    assert await count(engine, "recognition_events") == 1
    assert await count(engine, "face_embeddings") == 1


async def test_retention_days_are_configurable(engine):
    await zone_event(engine, NOW - timedelta(days=3))
    cfg = RetentionConfig(RETENTION_DAYS_EVENTS=2)
    deleted = await RetentionJob(cfg, engine=engine).run_once(now=NOW)
    assert deleted["zone_events"] == 1


def test_protected_tables_are_not_in_the_rules():
    tables = {r.table for r in RULES}
    assert tables.isdisjoint({"intruder_events", "headcount_breach_events"})
    (fe,) = [r for r in RULES if r.table == "face_embeddings"]
    assert "is_enrollment = false" in fe.extra_where


def test_defaults(monkeypatch):
    for k in ("RETENTION_DAYS_RAW", "RETENTION_DAYS_EVENTS", "RETENTION_INTERVAL_S",
              "RETENTION_BATCH_SIZE"):
        monkeypatch.delenv(k, raising=False)
    cfg = RetentionConfig(_env_file=None)
    assert (cfg.RETENTION_DAYS_RAW, cfg.RETENTION_DAYS_EVENTS) == (7, 30)
    assert (cfg.RETENTION_INTERVAL_S, cfg.RETENTION_BATCH_SIZE) == (3600, 10_000)
