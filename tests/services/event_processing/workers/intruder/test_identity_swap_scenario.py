"""Identity swap, end to end on REAL Postgres + REAL Redis.

The recognition consumer (fake model: faces placed in frame pixels) writes
real recognition rows; the intruder processor then classifies the zone
events of every track from those rows. Only the model is fake.
"""
import json
import math
import uuid
from datetime import timedelta

import numpy as np
import pytest

from tests.recognition_fakes import FrameFaceLoader, face_at, jpeg, person_track, unit

from services.recognition.src import consumer as rec_mod  # noqa: E402  (after the insightface stub)
from services.recognition.src.consumer import RecognitionConsumer  # noqa: E402

from .conftest import CAMERA, T0, alerts, settle, zone_event

W, H = 640, 480
FRAMES = 5

AUTHORIZED_FACE = unit(0)
UNKNOWN_FACE = unit(5)
# cosine 0.65 with the authorized person: between VISITOR (0.60) and ENROLLED (0.75)
LOW_SIMILARITY_FACE = (0.65 * unit(0) + math.sqrt(1 - 0.65**2) * unit(1)).astype(np.float32)


@pytest.fixture
async def recognizer(pg_session_factory, redis_client, monkeypatch):
    monkeypatch.setattr(rec_mod.config, "DEFAULT_MIN_FACE_SIZE_PX", 10)
    monkeypatch.setattr(rec_mod.config, "DEFAULT_BLUR_THRESHOLD", 0.0)
    rc = RecognitionConsumer()
    rc.redis = redis_client
    rc._minio = None
    rc._session_factory = pg_session_factory
    rc._sampler.sample_rate = 1  # every frame is a sample: FRAMES rows per track

    async def no_camera_config(_):
        return {}

    rc._cam_config.get = no_camera_config
    rc.loader = FrameFaceLoader([]).attach(rc)
    yield rc
    await rc._engine.dispose()
    await rc._cache._engine.dispose()
    await rc._cam_config._engine.dispose()


def enroll(rc, person_id) -> None:
    pid = str(person_id)
    rc._cache.enrolled_matrix = np.stack([AUTHORIZED_FACE]).astype(np.float32)
    rc._cache.enrolled_persons = [{"person_id": pid, "name": "Auth", "role": None,
                                   "department": None, "clearance_level": 1}]
    rc._cache.person_id_to_idx = {pid: 0}


async def run_frames(rc, tracks, faces_for_frame, frames=FRAMES) -> None:
    """Feed `frames` detection events (1 s apart) through recognition."""
    frame = jpeg(W, H)
    for i in range(frames):
        seq = i + 1
        rc.loader.faces = faces_for_frame(i)
        ref = f"frame:{CAMERA}:{seq}"
        await rc.redis.set(ref, frame)
        event = {
            "event_id": str(uuid.uuid4()),
            "camera_id": str(CAMERA),
            "frame_event_id": str(uuid.uuid4()),
            "timestamp": (T0 + timedelta(seconds=i)).isoformat(),
            "frame_reference": ref,
            "frame_provider": "redis",
            "frame_seq": seq,
            "frame_shape": [H, W],
            "inference_latency_ms": 1.0,
            "tracks": tracks,
        }
        await rc.process(f"{seq}-0", {b"data": json.dumps(event).encode()}, "events:detections")


async def enter_zone(make_processor, *track_ids) -> None:
    proc, _ = make_processor()
    for n, track_id in enumerate(track_ids):
        await proc.handle(zone_event(track=track_id, at=FRAMES, seq=100 + n))
    await settle(proc)


async def recognitions(db, track_id):
    return await db.rows(
        "SELECT identity_tag::text AS tag, person_id FROM recognition_events "
        "WHERE track_id = :t ORDER BY timestamp", t=track_id,
    )


async def test_neighbours_face_in_crop_does_not_authorize_intruder(
    recognizer, db, make_processor, redis_client,
):
    """A (unknown, face turned away) overlaps authorized B; A's crop holds B's
    face. Before the fix A was recorded as B and never alerted."""
    await db.zone()
    person = await db.person(authorized=True)
    enroll(recognizer, person)
    a = person_track(1, 0.20, 0.20, 0.55, 0.95)
    b = person_track(2, 0.45, 0.10, 0.80, 0.90)
    b_face = face_at(0.53, 0.20, W, H, AUTHORIZED_FACE, det_score=0.99)

    await run_frames(recognizer, [a, b], lambda i: [b_face])

    assert await recognitions(db, 1) == []  # A: nothing recorded, no guess
    assert all(r.person_id == person for r in await recognitions(db, 2))

    await enter_zone(make_processor, 1, 2)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["track_id"] == 1
    assert alert["metadata"]["classification_reason"] == "unidentified_in_restricted"


async def test_authorized_person_alone_never_alerts(recognizer, db, make_processor, redis_client):
    await db.zone()
    person = await db.person(authorized=True)
    enroll(recognizer, person)
    b = person_track(2, 0.40, 0.10, 0.75, 0.90)
    b_face = face_at(0.575, 0.18, W, H, AUTHORIZED_FACE)

    await run_frames(recognizer, [b], lambda i: [b_face])
    assert len(await recognitions(db, 2)) == FRAMES

    await enter_zone(make_processor, 2)

    assert await alerts(redis_client) == []


async def test_authorized_and_unknown_side_by_side_alert_once_for_unknown(
    recognizer, db, make_processor, redis_client,
):
    """Overlapping head regions, each face visible: exactly one alert, for A."""
    await db.zone()
    person = await db.person(authorized=True)
    enroll(recognizer, person)
    a = person_track(1, 0.30, 0.10, 0.60, 0.90)  # unknown
    b = person_track(2, 0.45, 0.10, 0.75, 0.90)  # authorized
    a_face = face_at(0.43, 0.15, W, H, UNKNOWN_FACE, det_score=0.80)
    b_face = face_at(0.53, 0.15, W, H, AUTHORIZED_FACE, det_score=0.99)  # in both heads

    await run_frames(recognizer, [a, b], lambda i: [a_face, b_face])

    assert {r.tag for r in await recognitions(db, 1)} == {"unknown"}
    assert {r.tag for r in await recognitions(db, 2)} == {"enrolled"}

    await enter_zone(make_processor, 1, 2)

    (alert,) = await alerts(redis_client)
    assert alert["metadata"]["track_id"] == 1
    assert alert["metadata"]["classification_reason"] == "unknown_in_restricted"


async def test_authorized_person_with_one_low_similarity_row_stays_authorized(
    recognizer, db, make_processor, redis_client,
):
    await db.zone()
    person = await db.person(authorized=True)
    enroll(recognizer, person)
    b = person_track(2, 0.40, 0.10, 0.75, 0.90)
    good = face_at(0.575, 0.18, W, H, AUTHORIZED_FACE)
    poor = face_at(0.575, 0.18, W, H, LOW_SIMILARITY_FACE)
    # 5 enrolled rows + 1 visitor row (a poorer frame)
    await run_frames(recognizer, [b], lambda i: [poor if i == 3 else good], frames=6)

    tags = [r.tag for r in await recognitions(db, 2)]
    assert sorted(tags) == ["enrolled"] * 5 + ["visitor"]

    await enter_zone(make_processor, 2)

    assert await alerts(redis_client) == []
