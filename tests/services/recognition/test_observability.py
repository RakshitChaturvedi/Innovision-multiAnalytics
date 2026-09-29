"""Recognition rejections are counted, logged and alarmed on.

Redis is REAL (local redis-server). The model and Postgres are FAKES
(FakeLoader / FakeDB); the real-Postgres side is in
tests/integration/test_recognition_real_pg.py.
"""
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.recognition.src import camera_config_store as ccs
from services.recognition.src import consumer as consumer_mod

from .conftest import FakeDB, FakeFace, detection_event, jpeg
from . import test_consumer
from .test_consumer import STREAM, FakeLoader, deliver

rc = test_consumer.rc  # same fixture: real Redis, FakeDB, fake model

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NEW_REASONS = (
    "crop_none", "precheck_too_small", "precheck_too_blurry",
    "quality_pose", "quality_detector_confidence", "quality_other", "rows_written",
)


class NoFaceLoader:
    def detect_faces(self, crop):
        return []


async def run_event(c, camera=None, ts=T0, frame_seq=1, face_bbox=None, track_id=7):
    camera = camera or uuid.uuid4()
    ev = detection_event(camera_id=camera, frame_seq=frame_seq, ts=ts)
    if face_bbox is not None:
        ev["tracks"][0]["face_bbox"] = face_bbox
    ev["tracks"][0]["track_id"] = track_id
    await c.redis.set(ev["frame_reference"], jpeg())
    msg_id, fields = await deliver(c, ev)
    await c._handle(STREAM, msg_id, fields)
    return str(camera), ev


def reason_logs(caplog, level):
    return [r for r in caplog.records
            if r.levelno == level and "recognition_rejected" in r.getMessage()]


# ------------------------------------------------------------------ counters


async def test_new_counters_are_in_stats_from_the_start(rc):
    stats = rc.stats()
    for name in NEW_REASONS:
        assert stats[name] == 0, name
    assert stats["per_camera"] == {}


@pytest.mark.parametrize("reason, setup", [
    ("crop_none", lambda c, mp: {"face_bbox": {"x1": 0.5, "y1": 0.5, "x2": 0.51, "y2": 0.51}}),
    ("precheck_too_small", lambda c, mp: mp.setattr(consumer_mod.config, "DEFAULT_MIN_FACE_SIZE_PX", 10_000)),
    ("precheck_too_blurry", lambda c, mp: mp.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)),
    ("face_no_face", lambda c, mp: setattr(c, "_model_loader", NoFaceLoader())),
    ("quality_pose", lambda c, mp: setattr(c, "_model_loader", FakeLoader(FakeFace(pose=(0.0, 80.0, 0.0))))),
    ("quality_detector_confidence", lambda c, mp: setattr(c, "_model_loader", FakeLoader(FakeFace(det_score=0.1)))),
    ("quality_other", lambda c, mp: setattr(c, "_model_loader", FakeLoader(FakeFace(embedding=None)))),
])
async def test_each_rejection_is_counted_per_camera(rc, monkeypatch, reason, setup):
    kwargs = setup(rc, monkeypatch) or {}
    camera, _ = await run_event(rc, **kwargs)

    stats = rc.stats()
    assert stats[reason] == 1
    assert stats["per_camera"][camera] == {reason: 1}
    assert stats["rows_written"] == 0
    assert rc._db.tables("recognition_events") == []


async def test_rows_written_counts_real_inserts_not_redeliveries(rc):
    camera, ev = await run_event(rc)
    assert rc.stats()["rows_written"] == 1
    # Redelivery of the same message: ON CONFLICT DO NOTHING, nothing new.
    rc._sampler.evict(camera, 7)
    msg_id, fields = await deliver(rc, ev)
    await rc._handle(STREAM, msg_id, fields)
    assert rc.stats()["rows_written"] == 1
    assert rc.stats()["per_camera"][camera] == {"rows_written": 1}


async def test_per_camera_counts_are_separate(rc, monkeypatch):
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)
    cam_a, _ = await run_event(rc)
    await run_event(rc, camera=uuid.UUID(cam_a), frame_seq=2, track_id=8)
    cam_b, _ = await run_event(rc)
    per_cam = rc.stats()["per_camera"]
    assert per_cam[cam_a] == {"precheck_too_blurry": 2}
    assert per_cam[cam_b] == {"precheck_too_blurry": 1}
    assert rc.stats()["precheck_too_blurry"] == 3


async def test_periodic_stats_line_includes_new_counters(rc, monkeypatch, caplog):
    """The one existing stats logger (BaseStreamConsumer._stats_loop) prints them."""
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)
    camera, _ = await run_event(rc)
    rc.stats_interval_s = 0.01
    rc._running = True
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(rc._stats_loop())
        await asyncio.sleep(0.05)
        rc._running = False
        await asyncio.wait_for(task, 1)
    lines = [r.getMessage() for r in caplog.records if " stats " in r.getMessage()]
    assert lines, "no stats line logged"
    assert "'precheck_too_blurry': 1" in lines[0]
    assert "'rows_written': 0" in lines[0]
    assert f"'{camera}': {{'precheck_too_blurry': 1}}" in lines[0]


# ------------------------------------------------------------------- logging


async def test_first_rejection_per_camera_and_reason_is_info_then_debug(rc, monkeypatch, caplog):
    """Before: rejections were logged at DEBUG only, so a gate rejecting every
    face was invisible at the default log level."""
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)
    cam = uuid.uuid4()
    with caplog.at_level(logging.DEBUG):
        for seq, track in ((1, 7), (2, 8), (3, 9)):
            await run_event(rc, camera=cam, frame_seq=seq, track_id=track)
        await run_event(rc)  # another camera: its first one is INFO again

    info = reason_logs(caplog, logging.INFO)
    debug = reason_logs(caplog, logging.DEBUG)
    assert len(info) == 2 and len(debug) == 2
    msg = info[0].getMessage()
    assert f"camera={cam}" in msg and "reason=precheck_too_blurry" in msg and "too_blurry:" in msg


# ---------------------------------------------------------------- starvation


def starved_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "recognition_starved" in r.getMessage()]


async def test_starvation_warns_once_after_threshold_in_event_time(rc, monkeypatch, caplog):
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)
    cam = uuid.uuid4()
    with caplog.at_level(logging.WARNING):
        for i, secs in enumerate((0, 60, 119, 121, 200, 300)):
            await run_event(rc, camera=cam, ts=T0 + timedelta(seconds=secs), frame_seq=i + 1, track_id=100 + i)
    warnings = starved_warnings(caplog)
    assert len(warnings) == 1
    assert f"camera={cam}" in warnings[0].getMessage()
    assert "top_reason=precheck_too_blurry" in warnings[0].getMessage()


async def test_starvation_resets_when_a_row_is_written(rc, monkeypatch, caplog):
    cam = uuid.uuid4()
    monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)
    with caplog.at_level(logging.WARNING):
        await run_event(rc, camera=cam, ts=T0, frame_seq=1, track_id=1)
        await run_event(rc, camera=cam, ts=T0 + timedelta(seconds=130), frame_seq=2, track_id=2)
        assert len(starved_warnings(caplog)) == 1
        monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 0.0)
        await run_event(rc, camera=cam, ts=T0 + timedelta(seconds=140), frame_seq=3, track_id=3)
        assert not rc._starvation.is_starved(str(cam))
        # A new outage warns again.
        monkeypatch.setattr(consumer_mod.config, "DEFAULT_BLUR_THRESHOLD", 1e12)
        await run_event(rc, camera=cam, ts=T0 + timedelta(seconds=150), frame_seq=4, track_id=4)
        await run_event(rc, camera=cam, ts=T0 + timedelta(seconds=280), frame_seq=5, track_id=5)
    assert len(starved_warnings(caplog)) == 2


async def test_starvation_names_frame_expired_when_frames_are_gone(rc, caplog):
    cam = uuid.uuid4()
    with caplog.at_level(logging.WARNING):
        for i, secs in enumerate((0, 130)):
            ev = detection_event(camera_id=cam, frame_seq=i + 1, ts=T0 + timedelta(seconds=secs))
            msg_id, fields = await deliver(rc, ev)  # frame never stored: expired
            await rc._handle(STREAM, msg_id, fields)
    warnings = starved_warnings(caplog)
    assert len(warnings) == 1 and "top_reason=frame_expired" in warnings[0].getMessage()


def test_starvation_ignores_wall_clock(monkeypatch, caplog):
    """Many events with the same event time never starve, however long it takes."""
    from services.recognition.src.starvation import StarvationMonitor

    mon = StarvationMonitor(starved_after_s=120)
    import time
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 10_000)
    monkeypatch.setattr(time, "monotonic", lambda: real + 10_000)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            mon.track_due("cam", T0)
            mon.rejected("cam", "crop_none")
    assert starved_warnings(caplog) == []


def test_starvation_needs_due_tracks_and_reports_top_reason(caplog):
    from services.recognition.src.starvation import StarvationMonitor

    mon = StarvationMonitor(starved_after_s=120)
    with caplog.at_level(logging.WARNING):
        mon.rejected("cam", "crop_none")  # no window yet: ignored
        mon.track_due("cam", T0)
        for _ in range(3):
            mon.rejected("cam", "quality_pose")
        mon.rejected("cam", "crop_none")
        mon.track_due("cam", T0 + timedelta(seconds=120))
        mon.track_due("cam", T0 + timedelta(seconds=500))
        mon.track_due("other", T0 + timedelta(seconds=500))  # other camera: own window
    warnings = starved_warnings(caplog)
    assert len(warnings) == 1
    assert "camera=cam " in warnings[0].getMessage()
    assert "top_reason=quality_pose" in warnings[0].getMessage()


def test_starvation_threshold_comes_from_config():
    assert consumer_mod.config.RECOGNITION_STARVED_AFTER_S == 120.0


# ------------------------------------------------ legacy per-camera threshold


@pytest.fixture
async def store():
    s = ccs.CameraConfigStore()
    s._db = FakeDB()
    s._session_factory = s._db
    yield s
    await s.close()


async def test_startup_warns_once_per_camera_still_at_legacy_blur_threshold(store, caplog):
    store._db.select_rows = [SimpleNamespace(camera_id="cam-a"), SimpleNamespace(camera_id="cam-b")]
    with caplog.at_level(logging.WARNING):
        await store.warn_legacy_blur_thresholds()
        await store.warn_legacy_blur_thresholds()
        # Loading the camera later does not repeat it.
        store._db.select_rows = [SimpleNamespace(
            similarity_threshold=0.6, min_face_size_px=40,
            blur_threshold=100.0, recognition_sample_rate=10)]
        await store.get("cam-a")
    msgs = [r.getMessage() for r in caplog.records if "camera_blur_threshold_legacy" in r.getMessage()]
    assert len(msgs) == 2
    assert any("camera_id=cam-a" in m for m in msgs) and any("camera_id=cam-b" in m for m in msgs)


async def test_legacy_threshold_found_on_load_warns_once(store, caplog):
    store._db.select_rows = [SimpleNamespace(
        similarity_threshold=0.6, min_face_size_px=40, blur_threshold=100.0, recognition_sample_rate=10)]
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            store.invalidate("cam")
            await store.get("cam")
    assert sum("camera_blur_threshold_legacy" in r.getMessage() for r in caplog.records) == 1


async def test_non_legacy_threshold_does_not_warn(store, caplog):
    store._db.select_rows = [SimpleNamespace(
        similarity_threshold=0.6, min_face_size_px=40, blur_threshold=15.0, recognition_sample_rate=10)]
    with caplog.at_level(logging.WARNING):
        await store.get("cam")
    assert not any("camera_blur_threshold_legacy" in r.getMessage() for r in caplog.records)

