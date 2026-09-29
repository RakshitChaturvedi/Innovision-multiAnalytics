"""RecognitionConsumer: frame fetch, similarity clamp, transaction, retries.

Redis is REAL (local redis-server). The model and Postgres are FAKES.
"""
import json
import logging
import uuid
from datetime import UTC, datetime

import numpy as np
import pytest

from services.recognition.src import consumer as consumer_mod
from services.recognition.src.consumer import RecognitionConsumer, _clamp01
from shared.errors import PermanentError
from shared.frames import FrameUnavailable
from shared.schemas.events import RecognitionEvent

from .conftest import FakeDB, FakeFace, detection_event, jpeg, unit

STREAM = "events:detections"


class FakeLoader:
    def __init__(self, face):
        self.face = face
        self.calls = 0

    def detect_best_face(self, crop):
        self.calls += 1
        return self.face


@pytest.fixture
async def rc(redis, monkeypatch):
    c = RecognitionConsumer()
    c.redis = redis
    c._minio = None
    c._db = FakeDB()
    c._session_factory = c._db
    c._model_loader = FakeLoader(FakeFace())
    # permissive gates: the synthetic noise frame is sharp; keep sizes small
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_MIN_FACE_SIZE_PX", 10)
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 0.0)
    async def cfg(_):
        return {}
    c._cam_config.get = cfg
    await redis.xgroup_create(STREAM, c.group_name, id="0", mkstream=True)
    yield c
    await c._engine.dispose()
    await c._cache._engine.dispose()
    await c._cam_config._engine.dispose()


async def deliver(c, payload) -> tuple[str, dict]:
    """XADD + XREADGROUP so the entry is really pending, like in production."""
    data = {"data": payload if isinstance(payload, str) else json.dumps(payload)}
    await c.redis.xadd(STREAM, data)
    res = await c.redis.xreadgroup(c.group_name, c.consumer_name, {STREAM: ">"}, count=1)
    msg_id, fields = res[0][1][0]
    return msg_id, fields


async def pending(c) -> int:
    return (await c.redis.xpending(STREAM, c.group_name))["pending"]


def set_match(c, score_vec, person_id=None):
    """Make the cache return a person whose row is `score_vec`."""
    pid = person_id or str(uuid.uuid4())
    c._cache.enrolled_matrix = np.stack([score_vec]).astype(np.float32)
    c._cache.enrolled_persons = [{"person_id": pid, "name": "P", "role": None,
                                  "department": None, "clearance_level": 1}]
    c._cache.person_id_to_idx = {pid: 0}
    return pid


# ---------------------------------------------------------------- 1. frames


async def test_no_module_level_storage_client():
    assert not hasattr(consumer_mod, "_storage")


async def test_frame_fetched_via_provider_and_reference_verbatim(rc):
    ev = detection_event(frame_seq=42)
    await rc.redis.set(ev["frame_reference"], jpeg())
    await rc.redis.set("frames:" + ev["frame_reference"], b"WRONG")  # prefixed key must never be read
    seen = {}

    async def spy(detection_event, track, frame, cam_cfg):
        seen["shape"] = frame.shape

    rc._process_track = spy
    msg_id, fields = await deliver(rc, ev)
    await rc._handle(STREAM, msg_id, fields)

    assert seen["shape"] == (240, 320, 3)
    assert await pending(rc) == 0


async def test_frame_unavailable_is_dead_lettered_counted_not_retried(rc, caplog):
    ev = detection_event(frame_seq=5)  # nothing stored in redis, no minio
    msg_id, fields = await deliver(rc, ev)
    with caplog.at_level(logging.WARNING):
        await rc._handle(STREAM, msg_id, fields)

    assert await pending(rc) == 0  # acked: no infinite retry
    dlq = await rc.redis.xrange(f"{STREAM}:dlq")
    assert len(dlq) == 1 and b"FrameUnavailable" in dlq[0][1][b"error"]
    assert rc.stats()["frame_unavailable"] == 1
    assert rc.stats()["dlq"] == 1
    assert any("frame_unavailable" in r.message for r in caplog.records)


async def test_process_raises_frame_unavailable(rc):
    msg_id, fields = await deliver(rc, detection_event())
    with pytest.raises(FrameUnavailable):
        await rc.process(msg_id, fields, STREAM)


async def test_bad_payload_and_corrupt_frame_are_permanent(rc):
    msg_id, fields = await deliver(rc, "{not json")
    with pytest.raises(PermanentError):
        await rc.process(msg_id, fields, STREAM)
    ev = detection_event()
    await rc.redis.set(ev["frame_reference"], b"this is not a jpeg")
    msg_id, fields = await deliver(rc, ev)
    with pytest.raises(PermanentError):
        await rc.process(msg_id, fields, STREAM)
    assert rc.stats()["bad_payload"] == 1 and rc.stats()["frame_decode_failed"] == 1


# ---------------------------------------------------------------- 2. clamp


def test_clamp01():
    assert _clamp01(-0.4) == 0.0
    assert _clamp01(1.3) == 1.0
    assert _clamp01(float("nan")) == 0.0
    assert _clamp01(0.5) == 0.5


async def test_negative_similarity_clamped_in_db_and_event(rc):
    ev = detection_event(frame_seq=3)
    await rc.redis.set(ev["frame_reference"], jpeg())
    set_match(rc, -unit(0))  # face embedding unit(0) -> cosine = -1.0
    msg_id, fields = await deliver(rc, ev)
    await rc._handle(STREAM, msg_id, fields)

    row = rc._db.tables("recognition_events")[0]
    assert row["similarity_score"] == 0.0
    assert row["identity_tag"] == "unknown"
    published = await rc.redis.xrange("events:recognitions")
    event = RecognitionEvent.model_validate_json(published[0][1][b"data"])
    assert event.similarity_score == 0.0
    assert await pending(rc) == 0


# --------------------------------------------------- 3. transaction / retries


async def test_whole_transaction_commits_together(rc):
    ev = detection_event(frame_seq=3)
    await rc.redis.set(ev["frame_reference"], jpeg())
    pid = set_match(rc, unit(0))
    msg_id, fields = await deliver(rc, ev)
    await rc._handle(STREAM, msg_id, fields)

    db = rc._db
    assert len(db.tables("face_embeddings")) == 1
    assert len(db.tables("recognition_events")) == 1
    assert len(db.tables("audit_log")) == 1
    assert any("UPDATE enrolled_persons" in sql for sql, _ in db.committed)
    assert db.rolled_back == []
    rec = db.tables("recognition_events")[0]
    assert rec["person_id"] == pid and rec["identity_tag"] == "enrolled"
    assert rec["similarity_score"] == pytest.approx(1.0)
    # event time, not wall clock
    assert rec["timestamp"] == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # audit metadata is serialized JSON for CAST(:metadata AS jsonb)
    assert json.loads(db.tables("audit_log")[0]["metadata"])["person_id"] == pid
    assert len(await rc.redis.xrange("events:recognitions")) == 1


async def test_failure_in_audit_rolls_back_everything_and_message_stays_pending(rc):
    ev = detection_event(frame_seq=3)
    await rc.redis.set(ev["frame_reference"], jpeg())
    rc._db.fail_on = "INSERT INTO audit_log"
    msg_id, fields = await deliver(rc, ev)
    await rc._handle(STREAM, msg_id, fields)

    assert rc._db.committed == []
    assert len(rc._db.rolled_back) >= 2  # embedding + event were rolled back
    assert await rc.redis.xlen("events:recognitions") == 0  # nothing published before commit
    assert await pending(rc) == 1  # retried later by the reclaim loop
    assert rc.stats()["failed"] == 1


async def test_retry_after_failure_is_idempotent(rc):
    ev = detection_event(frame_seq=3)
    await rc.redis.set(ev["frame_reference"], jpeg())
    rc._db.fail_on = "INSERT INTO audit_log"
    msg_id, fields = await deliver(rc, ev)
    await rc._handle(STREAM, msg_id, fields)
    rc._db.fail_on = None
    # first attempt inserted ids inside the rolled-back txn: emulate the DB
    rc._db.seen_ids.clear()
    await rc._handle(STREAM, msg_id, fields)  # redelivery
    await rc._handle(STREAM, msg_id, fields)  # duplicate redelivery
    assert len(rc._db.tables("recognition_events")) == 1
    assert len(rc._db.tables("audit_log")) == 1
    ids = {r[1][b"data"] for r in await rc.redis.xrange("events:recognitions")}
    assert len(ids) == 1  # same deterministic RecognitionEvent each time
    assert await pending(rc) == 0


async def test_track_failure_is_not_swallowed_and_track_is_resampled(rc):
    ev = detection_event(frame_seq=3)
    await rc.redis.set(ev["frame_reference"], jpeg())
    rc._db.fail_on = "INSERT INTO face_embeddings"
    msg_id, fields = await deliver(rc, ev)
    with pytest.raises(RuntimeError):
        await rc.process(msg_id, fields, STREAM)
    assert rc._sampler.active_tracks == 0  # evicted -> retry is not skipped by sampling
    rc._db.fail_on = None
    await rc.process(msg_id, fields, STREAM)
    assert len(rc._db.tables("recognition_events")) == 1


# ---------------------------------------------------------------- 10. stop()


async def test_stop_disposes_all_engines_and_cancels_tasks(rc):
    disposed = []

    class FakeEngine:
        def __init__(self, name):
            self.name = name

        async def dispose(self):
            disposed.append(self.name)

    rc._engine = FakeEngine("consumer")
    rc._cache._engine = FakeEngine("cache")
    rc._cam_config._engine = FakeEngine("camera")
    import asyncio
    rc._sweeper_task = asyncio.create_task(asyncio.sleep(3600))
    task = rc._sweeper_task
    await rc.stop()
    assert sorted(disposed) == ["cache", "camera", "consumer"]
    assert task.cancelled()
    await rc.stop()  # idempotent
