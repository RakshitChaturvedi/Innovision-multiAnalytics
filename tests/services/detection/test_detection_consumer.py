"""DetectionConsumer behaviour. Everything here runs on FAKES: fake YOLO
detector, IoU-based fake ByteTrack (tests/conftest.py), in-memory Redis and
MinIO stand-ins. No real Redis, Postgres, MinIO or model is involved."""
import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest
from minio.error import S3Error

from services.detection.src.config import settings
from services.detection.src.consumer import DetectionConsumer
from services.detection.src.detector import RawDetection
from services.detection.src.tracker import TrackerManager
from shared.errors import PermanentError
from shared.platform_contracts.enums import FrameProvider
from shared.schemas.events import DetectionEvent, FrameEvent

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CAM_A, CAM_B = str(uuid.uuid4()), str(uuid.uuid4())


class FakeRedis:
    """The one Redis used for frames, publishing, INCRBY and acks."""

    def __init__(self):
        self.frames: dict[str, bytes] = {}
        self.stream: list[tuple[str, dict]] = []
        self.acks: list[tuple[str, str]] = []
        self.counter = 0
        self.fail_xadd = 0
        self.get_delay = False

    async def get(self, key):
        if self.get_delay:
            await asyncio.sleep(random.random() * 0.01)
        return self.frames.get(key)

    async def xadd(self, stream, fields, **kwargs):
        if stream == "events:detections" and self.fail_xadd:
            self.fail_xadd -= 1
            raise ConnectionError("redis down")
        self.stream.append((stream, fields))
        return "1-0"

    async def xack(self, stream, group, msg_id):
        self.acks.append((stream, msg_id))

    async def incrby(self, key, n):
        self.counter += n
        return self.counter

    async def aclose(self):
        pass

    def events(self) -> list[DetectionEvent]:
        return [
            DetectionEvent.model_validate_json(f["data"])
            for s, f in self.stream
            if s == "events:detections"
        ]


class FakeStorage:
    """MinIO stand-in: object is always missing."""

    def download(self, bucket, key):
        raise S3Error(None, "NoSuchKey", "missing", key, "rid", "hid")


class FakeDetector:
    """Fake YOLO: one person whose box x-position is encoded in the frame
    (uniform gray level v -> box at x = 40*v), confidence from `conf_for`."""

    def __init__(self, conf=0.9):
        self.conf = conf
        self.batches: list[list[int]] = []

    def run_batch(self, frames):
        levels = [int(round(float(f.mean()) / 10)) for f in frames]
        self.batches.append(levels)
        out = []
        for v in levels:
            x = 40 * v
            out.append([RawDetection(x1=x, y1=50, x2=x + 60, y2=200,
                                     confidence=self.conf, class_id=0)])
        return out, 5.0


def jpeg(level: int) -> bytes:
    img = np.full((240, 320, 3), level * 10, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def frame_event(camera, seq, level=None, provider=FrameProvider.REDIS):
    return FrameEvent(
        camera_id=camera, timestamp=T0 + timedelta(seconds=seq), frame_seq=seq,
        frame_reference=f"frame:{camera}:{seq}", frame_provider=provider,
        frame_shape=(240, 320),
    )


def message(ev):
    return {"data": ev.model_dump_json()}


def make_consumer(redis=None, tracker_manager=None, detector=None):
    redis = redis or FakeRedis()
    with patch("services.detection.src.consumer.create_async_engine"):
        c = DetectionConsumer(
            detector=detector or FakeDetector(),
            tracker_manager=tracker_manager or TrackerManager(),
            side_redis=redis,
            storage=FakeStorage(),
        )
    from services.detection.src.publisher import DetectionPublisher
    from services.detection.src.track_ids import TrackIdAllocator

    c.redis = redis
    c._publisher = DetectionPublisher(redis)
    c._track_ids = TrackIdAllocator(redis, track_buffer=settings.track_buffer)
    c._write_to_db = AsyncMock()
    return c, redis


def put_frame(redis, ev, level):
    redis.frames[ev.frame_reference] = jpeg(level)


async def feed(c, redis, camera, seq, level=1, **kw):
    ev = frame_event(camera, seq, **kw)
    put_frame(redis, ev, level)
    await c.process(f"{seq}-0", message(ev), f"frames:{camera}")
    return ev


async def run_ready_batches(c):
    async with c._batch_manager._lock:
        await c._batch_manager._flush()
    while c._batch_manager.pending_batches():
        batch = await c._batch_manager.get_ready_batch()
        await c._process_batch(batch)
        c._batch_manager.task_done()


async def test_two_interleaved_cameras_keep_per_camera_order():
    detector = FakeDetector()
    c, redis = make_consumer(detector=detector)
    redis.get_delay = True  # random fetch latency tries to reorder frames

    items = []
    for seq in range(12):
        for cam, level in ((CAM_A, 1), (CAM_B, 6)):
            ev = frame_event(cam, seq)
            put_frame(redis, ev, level)
            items.append((f"frames:{cam}", f"{cam[:4]}-{seq}", message(ev)))
    # drive it through the base consumer's per-camera dispatch
    for i in range(0, len(items), 8):
        await c._dispatch(items[i:i + 8])
    await run_ready_batches(c)

    events = redis.events()
    assert len(events) == 24
    for cam in (CAM_A, CAM_B):
        seqs = [e.frame_seq for e in events if str(e.camera_id) == cam]
        assert seqs == list(range(12))
    # one persistent track per camera: order broken => new ids
    for cam in (CAM_A, CAM_B):
        ids = {t.track_id for e in events if str(e.camera_id) == cam for t in e.tracks}
        assert len(ids) == 1
    assert len({t.track_id for e in events for t in e.tracks}) == 2
    assert len(redis.acks) == 24


async def test_track_ids_differ_across_simulated_restart():
    redis = FakeRedis()  # the Redis counter survives the restart
    c1, _ = make_consumer(redis=redis)
    for seq in range(3):
        await feed(c1, redis, CAM_A, seq)
    await run_ready_batches(c1)
    before = {t.track_id for e in redis.events() for t in e.tracks}
    redis.stream.clear()

    c2, _ = make_consumer(redis=redis)  # new process: fresh ByteTrack, fresh map
    for seq in range(3, 6):
        await feed(c2, redis, CAM_A, seq)
    await run_ready_batches(c2)
    after = {t.track_id for e in redis.events() for t in e.tracks}

    assert len(before) == 1 and len(after) == 1
    assert not before & after


async def test_expired_frame_is_acked_and_counted_not_retried(caplog):
    c, redis = make_consumer()
    ev = frame_event(CAM_A, 7)  # nothing in Redis, MinIO copy missing too
    caplog.set_level(logging.WARNING)
    await c.process("7-0", message(ev), f"frames:{CAM_A}")  # must not raise

    assert redis.acks == [(f"frames:{CAM_A}", "7-0")]
    assert c.stats()["frame_expired"] == 1
    assert c._batch_manager.buffered_frames() == 0
    assert redis.events() == []
    assert any("frame_expired" in r.message for r in caplog.records)


async def test_low_confidence_tracked_box_is_still_published():
    c, redis = make_consumer(detector=FakeDetector(conf=0.35))  # < old 0.5 floor
    await feed(c, redis, CAM_A, 0)
    await run_ready_batches(c)
    (event,) = redis.events()
    assert len(event.tracks) == 1
    assert event.tracks[0].confidence == pytest.approx(0.35)


@pytest.mark.parametrize("provider", [FrameProvider.REDIS, FrameProvider.MINIO])
async def test_frame_provider_is_carried_to_detection_event(provider):
    c, redis = make_consumer()
    ev = frame_event(CAM_A, 0, provider=provider)
    if provider is FrameProvider.MINIO:
        # MinIO provider is fetched from MinIO only; serve it via the fake
        c._storage = type("S", (), {"download": lambda s, b, k: jpeg(1)})()
    else:
        put_frame(redis, ev, 1)
    await c.process("0-0", message(ev), f"frames:{CAM_A}")
    await run_ready_batches(c)
    (event,) = redis.events()
    assert event.frame_provider == provider
    assert event.frame_reference == ev.frame_reference


@pytest.mark.parametrize("data", [{}, {"data": "not json"}, {"data": b'{"camera_id": 1}'}])
async def test_malformed_message_raises_permanent_error(data):
    c, _ = make_consumer()
    with pytest.raises(PermanentError):
        await c.process("1-0", data, f"frames:{CAM_A}")
    assert c.stats()["invalid_payload"] == 1


async def test_malformed_message_goes_to_camera_dlq_and_is_acked():
    c, redis = make_consumer()
    stream = f"frames:{CAM_A}"
    await c._handle(stream, "1-0", {"data": "garbage"})
    dlq = [s for s, _ in redis.stream]
    assert dlq == [f"frames:{CAM_A}:dlq"]
    assert redis.acks == [(stream, "1-0")]


async def test_corrupt_jpeg_is_permanent():
    c, redis = make_consumer()
    ev = frame_event(CAM_A, 0)
    redis.frames[ev.frame_reference] = b"definitely not a jpeg"
    with pytest.raises(PermanentError):
        await c.process("0-0", message(ev), f"frames:{CAM_A}")
    assert c.stats()["corrupt_frame"] == 1


async def test_publish_failure_holds_camera_then_retry_republishes_in_order():
    c, redis = make_consumer()
    await feed(c, redis, CAM_A, 0)
    await feed(c, redis, CAM_A, 1)
    await feed(c, redis, CAM_A, 2)
    redis.fail_xadd = 1  # seq 0 fails to publish
    await run_ready_batches(c)

    # seq 1 and 2 must not overtake the unpublished seq 0
    assert redis.events() == [] and redis.acks == []
    assert c.stats()["publish_failed"] == 1 and c.stats()["deferred"] == 2
    tracker_updates_before = c._track_ids._updates[CAM_A]

    # base consumer redelivers (reclaim) in id order
    for seq in range(3):
        ev = frame_event(CAM_A, seq)
        await c.process(f"{seq}-0", message(ev), f"frames:{CAM_A}")
    await run_ready_batches(c)

    assert [e.frame_seq for e in redis.events()] == [0, 1, 2]
    assert len(redis.acks) == 3
    # seq 0 was republished from cache: the tracker saw it exactly once
    assert c._track_ids._updates[CAM_A] == tracker_updates_before + 2


async def test_redelivery_of_already_published_frame_is_acked_not_republished():
    c, redis = make_consumer()
    ev = await feed(c, redis, CAM_A, 0)
    await run_ready_batches(c)
    await c.process("0-0", message(ev), f"frames:{CAM_A}")  # reclaim, ack lost
    await run_ready_batches(c)
    assert len(redis.events()) == 1
    assert len(redis.acks) == 2
    assert c.stats()["duplicate_frame"] == 1


async def test_frame_seq_reset_with_newer_timestamp_resets_tracker():
    c, redis = make_consumer()
    for seq in (5, 6):
        await feed(c, redis, CAM_A, seq)
    await run_ready_batches(c)
    # source restarted its counter but time moves on
    ev = FrameEvent(camera_id=CAM_A, timestamp=T0 + timedelta(seconds=99), frame_seq=0,
                    frame_reference=f"frame:{CAM_A}:0", frame_provider=FrameProvider.REDIS,
                    frame_shape=(240, 320))
    put_frame(redis, ev, 1)
    await c.process("0-0", message(ev), f"frames:{CAM_A}")
    await run_ready_batches(c)
    events = redis.events()
    assert [e.frame_seq for e in events] == [5, 6, 0]
    assert events[2].tracks[0].track_id != events[0].tracks[0].track_id


async def test_stop_processes_ready_batches_before_cancelling_processor():
    c, redis = make_consumer()
    c._engine = AsyncMock()
    c._batch_manager.batch_size = 2
    await c._batch_manager.start()
    c._batch_processor_task = asyncio.create_task(c._batch_processor_loop())

    for seq in range(5):  # 2 full batches + 1 buffered frame
        await feed(c, redis, CAM_A, seq)
    await c.stop()

    assert [e.frame_seq for e in redis.events()] == [0, 1, 2, 3, 4]
    assert len(redis.acks) == 5
    assert c._batch_processor_task.done()


async def test_publish_and_ack_are_json_serialisable_events():
    c, redis = make_consumer()
    await feed(c, redis, CAM_A, 0)
    await run_ready_batches(c)
    json.loads(redis.stream[0][1]["data"])
