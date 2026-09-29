"""tools/check_integration.py exit codes against REAL Redis (db 15) and Postgres:
the analytics test database plus a throwaway 'platform' database with an alerts table."""
import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from shared.alerting.publisher import AlertPublisher, build_platform_alert
from tests.tools import fakes
from tools import check_integration as ci
from tools.platform.common import Reporter

PLATFORM_DB = "innovision_platform_tooltest"


class HealthStubs:
    """Three /health endpoints; status per service is settable."""

    def __init__(self):
        self.status = {"detection": 200, "recognition": 200, "event_processing": 200}
        self.body = {n: {"status": "ok", "seconds_since_last_message": 0.5, "consumers": []}
                     for n in self.status}
        self.servers = []
        self.ports = []
        for name in self.status:
            stub = self

            class H(BaseHTTPRequestHandler):
                svc = name

                def log_message(self, *a):
                    pass

                def do_GET(self):
                    raw = json.dumps(stub.body[self.svc]).encode()
                    self.send_response(stub.status[self.svc])
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)
            self.ports.append((name, srv.server_address[1]))

    def close(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()


@pytest.fixture
def health():
    h = HealthStubs()
    yield h
    h.close()


async def _admin(url, sql):
    import asyncpg

    u = make_url(url)
    conn = await asyncpg.connect(host=u.host, port=u.port or 5432, user=u.username,
                                 password=u.password, database="postgres")
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
async def platform_db(pg_url):
    await _admin(pg_url, f'DROP DATABASE IF EXISTS "{PLATFORM_DB}" WITH (FORCE)')
    await _admin(pg_url, f'CREATE DATABASE "{PLATFORM_DB}"')
    url = make_url(pg_url).set(database=PLATFORM_DB).render_as_string(hide_password=False)
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as c:
        await c.execute(text(
            "CREATE TABLE alerts (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), alert_id uuid NOT NULL, "
            "camera_id uuid, source_uc varchar(8), alert_type text, severity text, title text, "
            "description text, source_event_id uuid, frame_reference text, frame_provider text, "
            "status text, metadata jsonb, created_at timestamptz NOT NULL)"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
    await _admin(pg_url, f'DROP DATABASE IF EXISTS "{PLATFORM_DB}" WITH (FORCE)')


class World:
    """One healthy camera: frames, detections, a recognition row, one valid stored alert."""

    def __init__(self, registry, redis, pg, platform, health):
        self.registry, self.redis, self.pg, self.platform, self.health = registry, redis, pg, platform, health
        self.cam = registry.add("Phone 1")
        self.cam_id = self.cam["id"]

    async def frames(self, n=3):
        for i in range(n):
            await self.redis.xadd(f"frames:{self.cam_id}", {"data": "{}"})

    async def detections(self):
        await self.redis.xadd("events:detections", {"data": json.dumps({"camera_id": self.cam_id})})

    async def recognition(self):
        async with self.pg() as s:
            eid = uuid.uuid4()
            await s.execute(text(
                "INSERT INTO face_embeddings (id, embedding, quality_score) VALUES "
                "(:i, CAST(:e AS vector), 0.9)"), {"i": eid, "e": "[" + ",".join(["0.1"] * 512) + "]"})
            await s.execute(text(
                "INSERT INTO recognition_events (camera_id, detection_event_id, track_id, similarity_score, "
                "identity_tag, embedding_id, quality_score, timestamp) VALUES "
                "(:c, :d, 1, 0.2, 'unknown', :e, 0.9, now())"),
                {"c": self.cam_id, "d": uuid.uuid4(), "e": eid})
            await s.commit()

    def alert(self, **kw):
        return build_platform_alert(
            domain_event_id=kw.get("event_id", uuid.uuid4()), camera_id=uuid.UUID(kw.get("cam", self.cam_id)),
            timestamp=datetime.now(timezone.utc), severity="high", alert_type="intruder_detected",
            title="Intruder", description="unknown person", frame_seq=1)

    async def publish(self, alert, store=True):
        await AlertPublisher(self.redis).publish(alert)
        if store:
            async with self.platform() as s:
                await s.execute(text(
                    "INSERT INTO alerts (alert_id, camera_id, source_uc, created_at) VALUES (:a, :c, 'uc1', :t)"),
                    {"a": alert.alert_id, "c": alert.camera_id, "t": alert.timestamp})
                await s.commit()

    async def healthy(self):
        await self.frames()
        await self.detections()
        await self.recognition()
        await self.publish(self.alert())

    async def check(self, grace_s=0):
        r = Reporter()
        await ci.run(r, {"SOURCE_UC": "uc1"}, self.redis, self.pg, self.platform, window_s=600,
                     health_ports=self.health.ports, registry=self.registry.client(), grace_s=grace_s)
        return r


@pytest.fixture
async def world(registry, redis_client, pg_session_factory, platform_db, health):
    return World(registry, redis_client, pg_session_factory, platform_db, health)


async def test_healthy_is_exit_0(world, capsys):
    await world.healthy()
    r = await world.check()
    out = capsys.readouterr().out
    assert r.exit_code() == 0, out
    assert r.warned == 0, out
    assert "PASS frames Phone 1" in out and "PASS recognitions Phone 1: 1 rows" in out
    assert "all 1 published alert(s) stored by the platform" in out


async def test_no_frames_fails(world, capsys):
    await world.detections()
    r = await world.check()
    assert r.exit_code() == 1 and "FAIL frames Phone 1" in capsys.readouterr().out


async def test_no_recognitions_warns_with_top_reason(world, capsys):
    await world.frames()
    await world.detections()
    world.health.body["recognition"]["consumers"] = [{"per_camera": {world.cam_id: {
        "precheck_too_blurry": 40, "precheck_too_small": 3, "rows_written": 0}}}]
    r = await world.check()
    out = capsys.readouterr().out
    assert r.exit_code() == 0
    assert "WARN recognitions Phone 1" in out and "precheck_too_blurry (40x" in out


async def test_health_503_or_down_fails(world, capsys):
    await world.healthy()
    world.health.status["recognition"] = 503
    world.health.body["recognition"] = {"status": "unhealthy", "db": {"ok": False}}
    world.health.ports[2] = ("event_processing", 1)       # nothing listens
    r = await world.check()
    out = capsys.readouterr().out
    assert r.failed == 2 and "FAIL health recognition: HTTP 503" in out
    assert "FAIL health event_processing: not reachable" in out


async def test_invalid_alert_fails(world, capsys):
    await world.healthy()
    bad = world.alert(cam=str(uuid.uuid4()))            # camera not registered
    await world.publish(bad, store=True)
    await world.redis.xadd("alerts:live", {"data": json.dumps({"source_uc": "uc1", "title": ""})})
    r = await world.check()
    out = capsys.readouterr().out
    assert r.exit_code() == 1 and out.count("invalid:") == 2
    assert "not registered" in out


async def test_other_use_case_alerts_are_ignored(world, capsys):
    await world.healthy()
    await world.redis.xadd("alerts:live", {"data": json.dumps({"source_uc": "uc3", "title": ""})})
    r = await world.check()
    assert r.exit_code() == 0 and "1 from other use cases" in capsys.readouterr().out


async def test_duplicate_alert_id_fails(world, capsys):
    await world.healthy()
    a = world.alert()
    await world.publish(a)
    await AlertPublisher(world.redis).publish(a)         # a retry published it again
    r = await world.check()
    assert r.exit_code() == 1 and "published 2 times" in capsys.readouterr().out


async def test_alert_not_stored_by_platform_fails(world, capsys):
    await world.healthy()
    await world.publish(world.alert(), store=False)
    r = await world.check()
    out = capsys.readouterr().out
    assert r.exit_code() == 1 and "1 of 2 published alert(s) not stored" in out
    assert "alerts:dead_letter" in out


async def test_fresh_alert_within_grace_is_not_required(world):
    await world.healthy()
    await world.publish(world.alert(), store=False)
    assert (await world.check(grace_s=60)).exit_code() == 0


async def test_old_pending_and_dlq_fail(world, capsys):
    await world.healthy()
    old = int(time.time() * 1000) - 120_000
    await world.redis.xadd("events:zone", {"data": "{}"}, id=f"{old}-0")
    await world.redis.xgroup_create("events:zone", "intruder_group", id="0")
    await world.redis.xreadgroup("intruder_group", "c1", {"events:zone": ">"})
    await world.redis.xadd(f"frames:{world.cam_id}:dlq", {"data": "x", "error": "bad " + fakes.assignment("token", "dlq")})
    r = await world.check()
    out = capsys.readouterr().out
    assert r.failed == 2
    assert "FAIL pending events:zone/intruder_group" in out
    assert f"FAIL dlq frames:{world.cam_id}:dlq" in out and fakes.MARKER not in out


async def test_check_creates_no_consumer_group(world):
    await world.healthy()
    await world.check()
    for stream in ("alerts:live", "events:detections", f"frames:{world.cam_id}"):
        assert await world.redis.xinfo_groups(stream) == []


async def test_platform_query_is_read_only_and_identifiers_checked(world, capsys):
    await world.healthy()
    r = Reporter()
    await ci.check_platform_alerts(r, world.platform, {}, "uc1", 600, table="alerts; DROP TABLE alerts",
                                   id_col="alert_id", uc_col="source_uc", time_col="created_at",
                                   slack_s=0, grace_s=0, now=time.time())
    assert r.failed == 1 and "invalid identifier" in capsys.readouterr().out
    async with world.platform() as s:
        assert (await s.execute(text("SELECT count(*) FROM alerts"))).scalar() == 1


async def test_platform_columns_are_overridable(world, capsys):
    await world.healthy()
    r = Reporter()
    await ci.run(r, {"SOURCE_UC": "uc1"}, world.redis, world.pg, world.platform, window_s=600,
                 health_ports=world.health.ports, registry=world.registry.client(),
                 time_col="missing_column")
    assert r.exit_code() == 1 and "cannot read alerts" in capsys.readouterr().out
