"""tools/mock_platform + tools/check_health.py against REAL Redis (TEST_REDIS_DB)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np

from shared.alerting.publisher import AlertPublisher, build_platform_alert
from shared.frames import minio_frame_key
from shared.platform_contracts.enums import FrameProvider
from shared.platform_contracts.frame_event import FrameEvent
from tools import check_health
from tools.mock_platform import feeder
from tools.mock_platform.alert_sink import GROUP, STREAM, AlertSink

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _jpeg(level=5) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((240, 320, 3), level * 10, np.uint8))
    return buf.tobytes()


class RecordingStorage:
    def __init__(self):
        self.objects = {}

    def upload(self, bucket_key, key, data, content_type="application/octet-stream"):
        self.objects[(bucket_key, key)] = data
        return key


def _alert(cam, **kw):
    args = dict(
        domain_event_id=uuid.uuid4(), camera_id=cam, timestamp=T0, severity="high",
        alert_type="intruder_detected", title="Intruder", description="unknown person",
        frame_seq=3,
    )
    args.update(kw)
    return build_platform_alert(**args)


# ---------------------------------------------------------------- feeder

async def test_feeder_publishes_platform_frame_event_with_verbatim_key(redis_client):
    cam = uuid.uuid4()
    storage = RecordingStorage()
    jpeg = _jpeg()
    await feeder.publish_frame(redis_client, storage, camera_id=cam, frame_seq=7,
                               timestamp=T0, jpeg=jpeg, shape=(240, 320))

    [(_, fields)] = await redis_client.xrange(f"frames:{cam}")
    assert set(fields) == {b"data"}
    ev = FrameEvent.model_validate_json(fields[b"data"])
    assert ev.camera_id == cam and ev.frame_seq == 7 and ev.frame_shape == (240, 320)
    assert ev.frame_provider == FrameProvider.REDIS
    assert ev.frame_reference == f"frame:{cam}:7"
    assert await redis_client.get(ev.frame_reference) == jpeg  # GET verbatim works
    assert 0 < await redis_client.ttl(ev.frame_reference) <= feeder.FRAME_TTL_S
    assert storage.objects == {("frames", minio_frame_key(cam, 7)): jpeg}


async def test_feeder_frames_are_consumed_by_detection(redis_client):
    """Wiring: feeder output -> DetectionConsumer (fake YOLO) -> events:detections."""
    from services.detection.src.consumer import DetectionConsumer
    from services.detection.src.publisher import DetectionPublisher
    from services.detection.src.track_ids import TrackIdAllocator
    from services.detection.src.tracker import TrackerManager
    from shared.schemas.events import DetectionEvent
    from tests.services.detection.test_detection_consumer import FakeDetector, run_ready_batches

    cam = uuid.uuid4()
    for seq in range(3):
        await feeder.publish_frame(redis_client, None, camera_id=cam, frame_seq=seq,
                                   timestamp=T0 + timedelta(seconds=seq),
                                   jpeg=_jpeg(), shape=(240, 320))
    with patch("services.detection.src.consumer.create_async_engine"):
        c = DetectionConsumer(detector=FakeDetector(), tracker_manager=TrackerManager(),
                              side_redis=redis_client, storage=None)
    c.redis = redis_client
    c._publisher = DetectionPublisher(redis_client)
    c._track_ids = TrackIdAllocator(redis_client, track_buffer=30)
    c._write_to_db = AsyncMock()

    for msg_id, fields in await redis_client.xrange(f"frames:{cam}"):
        await c.process(msg_id.decode(), fields, f"frames:{cam}")
    await run_ready_batches(c)

    events = [DetectionEvent.model_validate_json(f[b"data"])
              for _, f in await redis_client.xrange("events:detections")]
    assert [e.frame_seq for e in events] == [0, 1, 2]
    assert all(str(e.camera_id) == str(cam) for e in events)
    assert all(e.tracks for e in events)


async def test_feeder_bad_payloads_are_rejected_by_detection(redis_client):
    from pydantic import ValidationError

    cam = uuid.uuid4()
    await feeder.publish_bad(redis_client, cam, 2)
    entries = await redis_client.xrange(f"frames:{cam}")
    assert len(entries) == 2
    for _, f in entries:
        try:
            FrameEvent.model_validate_json(f[b"data"])
        except ValidationError:
            continue
        raise AssertionError("injected payload validated")


# ---------------------------------------------------------------- alert sink

async def test_sink_counts_valid_duplicate_and_invalid_and_acks_all(redis_client):
    cam = uuid.uuid4()
    sink = AlertSink(redis_client, {cam})
    await sink.ensure_group()
    await sink.ensure_group()  # idempotent (BUSYGROUP)

    pub = AlertPublisher(redis_client)
    good = _alert(cam)
    await pub.publish(good)
    await pub.publish(good)                         # retry: same alert_id
    await pub.publish(_alert(uuid.uuid4()))         # unregistered camera
    await redis_client.xadd(STREAM, {"data": '{"title": ""}'})  # not an AlertEvent
    await redis_client.xadd(STREAM, {"other": "x"})             # no data field

    assert await sink.poll(block_ms=100) == 5
    assert (sink.valid, sink.duplicates, sink.invalid) == (1, 1, 3)
    assert any("not registered" in e for e in sink.errors)
    assert (await redis_client.xpending(STREAM, GROUP))["pending"] == 0


async def test_sink_accepts_alert_without_frame(redis_client):
    cam = uuid.uuid4()
    sink = AlertSink(redis_client, {cam})
    await sink.ensure_group()
    await AlertPublisher(redis_client).publish(_alert(cam, frame_seq=None))
    await sink.poll(block_ms=100)
    assert (sink.valid, sink.invalid) == (1, 0)


async def test_sink_processes_own_pending_after_restart(redis_client):
    cam = uuid.uuid4()
    sink = AlertSink(redis_client, {cam})
    await sink.ensure_group()
    await AlertPublisher(redis_client).publish(_alert(cam))
    # delivered to this consumer name but never acked (crash)
    await redis_client.xreadgroup(GROUP, sink.consumer, {STREAM: ">"})
    restarted = AlertSink(redis_client, {cam})
    assert await restarted.poll(block_ms=100) == 1
    assert restarted.valid == 1
    assert (await redis_client.xpending(STREAM, GROUP))["pending"] == 0


# ---------------------------------------------------------------- check_health

async def test_health_reports_old_pending_lag_and_dlq(redis_client):
    cam = uuid.uuid4()
    stream = f"frames:{cam}"
    await redis_client.xadd(stream, {"data": "x"}, id="1000-0")
    await redis_client.xadd(stream, {"data": "y"}, id="2000-0")
    await redis_client.xgroup_create(stream, "detection_group", id="0")
    await redis_client.xreadgroup("detection_group", "c1", {stream: ">"}, count=1)
    await redis_client.xadd(f"{stream}:dlq", {"data": "bad", "error": "e"})
    await redis_client.xadd("events:detections", {"data": "z"})
    await redis_client.xgroup_create("events:detections", "zone_monitor_group", id="$")

    report = await check_health.collect(redis_client, now_ms=91_000)

    assert report.streams == {stream: 2, "events:detections": 1}
    by = {(g.stream, g.group): g for g in report.groups}
    g = by[(stream, "detection_group")]
    assert (g.pending, g.oldest_pending_age_s, g.lag) == (1, 90.0, 1)
    z = by[("events:detections", "zone_monitor_group")]
    assert (z.pending, z.oldest_pending_age_s) == (0, None)
    assert report.dlq == {f"{stream}:dlq": 1}
    assert f"{stream}:dlq" not in report.streams

    problems = report.problems(60)
    assert len(problems) == 2
    assert report.problems(120, allow_dlq=1) == []
    assert "detection_group" in check_health.render(report)


async def test_health_is_ok_on_empty_redis(redis_client):
    report = await check_health.collect(redis_client)
    assert report.problems(60) == []
