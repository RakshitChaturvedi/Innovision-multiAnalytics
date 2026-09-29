import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from services.event_processing.src.workers.intruder.processor import (
    IntruderProcessor,
)
from services.event_processing.src.workers.intruder.repository import (
    IntruderRepository,
)
from shared.alerting.publisher import AlertPublisher
from shared.schemas.enums import EventType
from shared.schemas.events import ZoneEvent

CAMERA = UUID("11111111-1111-1111-1111-111111111111")
ZONE = UUID("22222222-2222-2222-2222-222222222222")
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
VEC = "[" + ",".join(["0.1"] * 512) + "]"


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


class FlakyPublisher:
    """Real AlertPublisher (real Redis) that fails the first N calls."""

    def __init__(self, real, fail_times=0):
        self.real, self.fail_times, self.attempts = real, fail_times, 0

    async def publish(self, alert):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ConnectionError("redis down")
        await self.real.publish(alert)


def zone_event(kind=EventType.ENTERED, track=7, at=0.0, seq=10, zone=ZONE):
    return ZoneEvent(
        camera_id=CAMERA,
        zone_id=zone,
        frame_seq=seq,
        track_id=track,
        timestamp=T0 + timedelta(seconds=at),
        event_type=kind,
        frame_reference=f"frame:{CAMERA}:{seq}",
    )


class Db:
    """Seeding + inspection helpers on the real test database."""

    def __init__(self, factory):
        self.f = factory

    async def run(self, sql, **params):
        async with self.f() as s, s.begin():
            return await s.execute(text(sql), params)

    async def rows(self, sql, **params):
        async with self.f() as s:
            return (await s.execute(text(sql), params)).all()

    async def zone(self, type_="restricted", zone_id=ZONE, name="Vault"):
        await self.run(
            "INSERT INTO zones (id, camera_id, name, type, polygon) VALUES "
            "(CAST(:id AS uuid), CAST(:c AS uuid), :n, :t, CAST(:p AS jsonb))",
            id=str(zone_id), c=str(CAMERA), n=name, t=type_,
            p=json.dumps([[0, 0], [1, 0], [1, 1]]),
        )

    async def person(self, authorized=False, blocklisted=False):
        pid = uuid4()
        await self.run(
            "INSERT INTO enrolled_persons (id, name) VALUES (CAST(:i AS uuid), 'p')",
            i=str(pid),
        )
        if authorized:
            await self.run(
                "INSERT INTO zone_authorized_persons (zone_id, person_id) "
                "VALUES (CAST(:z AS uuid), CAST(:p AS uuid))",
                z=str(ZONE), p=str(pid),
            )
        if blocklisted:
            await self.run(
                "INSERT INTO blocklist (person_id, reason) VALUES (CAST(:p AS uuid), 'x')",
                p=str(pid),
            )
        return pid

    async def recognition(self, tag, person=None, at=0.0, sim=0.9, track=7):
        emb = uuid4()
        await self.run(
            "INSERT INTO face_embeddings (id, person_id, embedding, quality_score) "
            "VALUES (CAST(:i AS uuid), CAST(:p AS uuid), CAST(:v AS vector), 0.9)",
            i=str(emb), p=str(person) if person else None, v=VEC,
        )
        await self.run(
            "INSERT INTO recognition_events (camera_id, detection_event_id, track_id, "
            "person_id, similarity_score, identity_tag, embedding_id, quality_score, timestamp) "
            "VALUES (CAST(:c AS uuid), CAST(:d AS uuid), :t, CAST(:p AS uuid), :s, "
            "CAST(:tag AS identity_tag), CAST(:e AS uuid), 0.9, :ts)",
            c=str(CAMERA), d=str(uuid4()), t=track, p=str(person) if person else None,
            s=sim, tag=tag, e=str(emb), ts=T0 + timedelta(seconds=at),
        )

    async def intruder_events(self):
        return await self.rows(
            "SELECT classification_reason, alert_status, alert_published, alert_id, "
            "resolved_at, person_id FROM intruder_events ORDER BY first_detected_at"
        )


@pytest.fixture
def db(pg_session_factory):
    return Db(pg_session_factory)


@pytest.fixture
def sleeps():
    return Sleeps()


@pytest.fixture
def make_processor(pg_session_factory, redis_client, sleeps):
    def _make(fail_times=0):
        publisher = FlakyPublisher(AlertPublisher(redis_client), fail_times)
        proc = IntruderProcessor(
            IntruderRepository(pg_session_factory), publisher, sleep=sleeps
        )
        return proc, publisher
    return _make


async def alerts(redis_client):
    """Everything published on alerts:live as parsed dicts."""
    return [
        json.loads(fields[b"data"])
        for _id, fields in await redis_client.xrange("alerts:live")
    ]
