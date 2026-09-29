"""Headcount against REAL Postgres and Redis.

Skipped unless HEADCOUNT_TEST_DATABASE_URL (postgresql+asyncpg://...) and
HEADCOUNT_TEST_REDIS_URL (redis://host:port/db) are set. Runs the full Alembic graph (needs pgvector) against the target database.
"""
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from services.event_processing.src.workers.headcount import headcount_store
from services.event_processing.src.workers.headcount.config import config
from services.event_processing.src.workers.headcount.consumer import HeadcountConsumer
from shared.alerting.publisher import AlertPublisher
from shared.platform_contracts.alert_event import AlertEvent
from tests.services.event_processing.workers.headcount.test_headcount_consumer import (
    ZONE, FakeZones, alert_id_for, make_cfg, payload, T0,
)

DB_URL = os.environ.get("HEADCOUNT_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("HEADCOUNT_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not (DB_URL and REDIS_URL), reason="real Postgres/Redis not configured"
)

ROOT = Path(__file__).resolve().parents[2]


def _alembic(monkeypatch, *args):
    """Run the real Alembic graph (needs pgvector: 0001-0003 use it)."""
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    getattr(command, args[0])(cfg, *args[1:])


def _reset_schema(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def sync_engine(monkeypatch):
    engine = create_engine(DB_URL.replace("+asyncpg", "+psycopg2"))
    _reset_schema(engine)
    _alembic(monkeypatch, "upgrade", "head")
    yield engine
    engine.dispose()


@pytest.fixture
async def store(monkeypatch, sync_engine):
    monkeypatch.setattr(config, "DATABASE_URL", DB_URL)
    s = headcount_store.HeadcountStore()
    yield s
    await s.dispose()


@pytest.fixture
async def redis_client():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.flushdb()
    yield r
    await r.aclose()


def test_migration_upgrade_downgrade_roundtrip(monkeypatch, sync_engine):
    _alembic(monkeypatch, "downgrade", "base")
    _alembic(monkeypatch, "upgrade", "head")


def test_migration_supersedes_duplicate_open_rows(monkeypatch):
    engine = create_engine(DB_URL.replace("+asyncpg", "+psycopg2"))
    _reset_schema(engine)
    _alembic(monkeypatch, "upgrade", "0005_headcount")
    z, c = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        for i in range(3):  # old code could leave several 'pending' rows per zone
            conn.execute(text(
                "INSERT INTO headcount_breach_events (id, zone_id, camera_id, count, threshold, timestamp) "
                "VALUES (gen_random_uuid(), :z, :c, 12, 10, now() + make_interval(secs => :i))"
            ), dict(z=z, c=c, i=i))
    _alembic(monkeypatch, "upgrade", "head")
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM headcount_breach_events WHERE status='open'")).scalar()
    assert n == 1
    engine.dispose()


async def test_open_breach_conflict_returns_same_row(store, sync_engine):
    z, c = str(uuid.uuid4()), str(uuid.uuid4())
    a = await store.open_breach(z, c, 12, 10, T0)
    b = await store.open_breach(z, c, 13, 10, T0 + timedelta(seconds=1))
    assert a.id == b.id and a.alert_published is False
    aid = str(uuid.uuid4())
    await store.mark_alert_published(a.id, aid)
    with sync_engine.connect() as conn:
        assert str(conn.execute(text(
            "SELECT alert_id FROM headcount_breach_events WHERE id = CAST(:i AS uuid)"), dict(i=a.id)).scalar()) == aid
    assert (await store.open_breach(z, c, 12, 10, T0)).alert_published is True
    assert [o.id for o in await store.load_open_breaches()] == [a.id]

    await store.resolve_breach(a.id, T0 + timedelta(seconds=30), "below_threshold")
    assert await store.load_open_breaches() == []
    with sync_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT status, alert_status, resolution_reason, resolved_at FROM headcount_breach_events"
        )).one()
    assert tuple(row)[:3] == ("resolved", "resolved", "below_threshold") and row[3] is not None
    d = await store.open_breach(z, c, 12, 10, T0 + timedelta(seconds=60))
    assert d.id != a.id


async def test_snapshot_is_idempotent(store, sync_engine):
    z, c = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(2):
        await store.write_snapshot(c, z, 4, 3.5, T0)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM headcount_snapshots")).scalar() == 1


async def test_end_to_end_one_alert_restart_and_current_key(store, redis_client):
    cfg = make_cfg()
    alerts = AlertPublisher(redis_client)

    def build():
        return HeadcountConsumer(
            cfg=cfg, zone_store=FakeZones(), repo=store, cache=redis_client,
            alert_publisher=alerts,
        )

    c1 = build()
    for t in range(0, 12):
        await c1.process(f"m{t}", payload(t, 12, seq=t), "events:detections")

    entries = await redis_client.xrange("alerts:live")
    assert len(entries) == 1
    alert = AlertEvent.model_validate_json(entries[0][1]["data"])
    (open_breach,) = await store.load_open_breaches()
    assert alert.alert_id == alert_id_for(open_breach.id) and open_breach.alert_published

    cur = json.loads(await redis_client.get(f"headcount:current:{ZONE}"))
    assert cur["count"] == 12
    assert 0 < await redis_client.ttl(f"headcount:current:{ZONE}") <= 60

    c2 = build()  # restart
    await c2.load_state()
    for t in range(100, 130):
        await c2.process(f"r{t}", payload(t, 12, seq=t), "events:detections")
    assert len(await redis_client.xrange("alerts:live")) == 1

    for t in range(130, 150):
        await c2.process(f"r{t}", payload(t, 4, seq=t), "events:detections")
    assert await store.load_open_breaches() == []
