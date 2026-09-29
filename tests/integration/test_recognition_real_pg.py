"""Blur recalibration and recognition counters against REAL Postgres (pgvector) and REAL Redis.

Uses its own database derived from TEST_DATABASE_URL (name + "_blur"),
dropped and recreated per test; skipped when Postgres is unreachable. The
face model is a FAKE (no weights here); frames are synthetic
(tools/blur_calibration.py).
"""
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.recognition_fakes import FakeFace  # first: installs the insightface import stub
from services.recognition.src import consumer as consumer_mod  # noqa: E402
from services.recognition.src.camera_config_store import CameraConfigStore
from services.recognition.src.consumer import RecognitionConsumer
from tests.services.recognition import conftest as recognition_conftest
from tools.blur_calibration import FACE_BOX, platform_cases

# Fixtures: a throwaway REAL redis-server, as in the recognition tests.
redis_port = recognition_conftest.redis_port
redis = recognition_conftest.redis

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/innovision_analytics_test",
)
STREAM = "events:detections"


@pytest.fixture
def db_url(monkeypatch):
    parsed = make_url(BASE_URL)
    url = parsed.set(database=f"{parsed.database}_blur")
    admin_url = parsed.set(drivername="postgresql+psycopg2", database="postgres")
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
        admin.dispose()
    except Exception as exc:
        pytest.skip(f"no Postgres at {BASE_URL}: {exc}")
    rendered = url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", rendered)
    return rendered


def alembic(*args):
    """In a subprocess: in-process alembic runs logging.fileConfig, which
    rewires the root logger (and caplog) for the rest of the test."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", *args],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def sync(url):
    return create_engine(url.replace("+asyncpg", "+psycopg2"))


def thresholds(engine) -> dict[str, float]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT camera_id, blur_threshold FROM camera_config"))
        return {str(r.camera_id): r.blur_threshold for r in rows}


def insert_camera(engine, blur=None) -> str:
    cam = str(uuid.uuid4())
    with engine.begin() as conn:
        if blur is None:  # server default
            conn.execute(text("INSERT INTO camera_config (camera_id) VALUES (:c)"), {"c": cam})
        else:
            conn.execute(text("INSERT INTO camera_config (camera_id, blur_threshold) VALUES (:c, :b)"),
                         {"c": cam, "b": blur})
    return cam


# ----------------------------------------------------------------- migration


def test_0008_moves_only_untouched_defaults_and_changes_server_default(db_url):
    alembic("upgrade", "0007_headcount_breach_state")
    engine = sync(db_url)
    legacy_default = insert_camera(engine)  # server default 100.0
    legacy_explicit = insert_camera(engine, 100.0)
    tuned = insert_camera(engine, 42.0)
    assert thresholds(engine)[legacy_default] == 100.0

    alembic("upgrade", "head")
    after = thresholds(engine)
    assert after[legacy_default] == 15.0
    assert after[legacy_explicit] == 15.0
    assert after[tuned] == 42.0
    new_cam = insert_camera(engine)
    assert thresholds(engine)[new_cam] == 15.0  # new server default

    alembic("downgrade", "0007_headcount_breach_state")
    assert thresholds(engine)[tuned] == 42.0
    assert thresholds(engine)[legacy_default] == 15.0  # rows are not rewritten back
    new_cam = insert_camera(engine)
    assert thresholds(engine)[new_cam] == 100.0  # old server default back
    alembic("upgrade", "head")
    engine.dispose()


def test_new_default_matches_service_config(db_url):
    alembic("upgrade", "head")
    engine = sync(db_url)
    new_cam = insert_camera(engine)
    assert thresholds(engine)[new_cam] == consumer_mod.config.DEFAULT_BLUR_THRESHOLD
    engine.dispose()


async def test_startup_legacy_check_runs_its_sql_against_real_pg(db_url, caplog):
    alembic("upgrade", "head")
    engine = sync(db_url)
    legacy = insert_camera(engine, 100.0)  # e.g. set by hand after the migration
    insert_camera(engine, 15.0)
    engine.dispose()

    store = CameraConfigStore()
    await store._engine.dispose()
    store._engine = create_async_engine(db_url, poolclass=NullPool)
    store._session_factory = async_sessionmaker(store._engine, expire_on_commit=False)
    with caplog.at_level(logging.WARNING):
        await store.warn_legacy_blur_thresholds()
    await store.close()
    msgs = [r.getMessage() for r in caplog.records if "camera_blur_threshold_legacy" in r.getMessage()]
    assert len(msgs) == 1 and f"camera_id={legacy}" in msgs[0]


# ------------------------------------------- consumer: 1080p frame -> a real row


class WholeCropLoader:
    """Fake model: one confident, frontal face filling the crop."""

    def detect_faces(self, crop):
        h, w = crop.shape[:2]
        return [FakeFace(bbox=(0.0, 0.0, float(w), float(h)))]


def platform_frame() -> bytes:
    """A sharp face from a 640x360 source, delivered by the platform as 1920x1080:
    the case the old raw-crop score rejected (72.8 < 100)."""
    case = next(c for c in platform_cases() if c.name == "sharp 640x360 -> platform 1920x1080")
    frame = np.full((1080, 1920, 3), 100, np.uint8)
    x1, y1, x2, y2 = FACE_BOX
    fy1, fx1 = int(y1 * 1080), int(x1 * 1920)
    frame[fy1:fy1 + case.crop.shape[0], fx1:fx1 + case.crop.shape[1]] = case.crop
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    assert ok
    return buf.tobytes()


def detection_payload(camera: str, seq: int) -> dict:
    x1, y1, x2, y2 = FACE_BOX
    person = {"x1": x1 - 0.03, "y1": y1 - 0.01, "x2": x2 + 0.03, "y2": 0.9}
    face = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    return {
        "event_id": str(uuid.uuid4()),
        "camera_id": camera,
        "frame_event_id": str(uuid.uuid4()),
        "timestamp": datetime(2026, 1, 1, 12, 0, seq, tzinfo=UTC).isoformat(),
        "frame_reference": f"frame:{camera}:{seq}",
        "frame_provider": "redis",
        "frame_seq": seq,
        "frame_shape": [1080, 1920],
        "inference_latency_ms": 1.0,
        "tracks": [{"track_id": 1, "bbox": person, "confidence": 0.9, "class_label": "person",
                    "has_face": True, "face_bbox": face}],
    }


async def test_1080p_platform_frame_writes_a_row_and_counts_it(db_url, redis):
    alembic("upgrade", "head")
    engine = sync(db_url)
    camera = insert_camera(engine)  # server default: the new threshold
    engine.dispose()

    c = RecognitionConsumer()
    for eng in (c._engine, c._cache._engine, c._cam_config._engine):
        await eng.dispose()
    c._engine = create_async_engine(db_url, poolclass=NullPool)
    c._session_factory = async_sessionmaker(c._engine, expire_on_commit=False)
    c._cam_config._engine = create_async_engine(db_url, poolclass=NullPool)
    c._cam_config._session_factory = async_sessionmaker(c._cam_config._engine, expire_on_commit=False)
    c.redis = redis
    c._minio = None
    c._model_loader = WholeCropLoader()
    await redis.xgroup_create(STREAM, c.group_name, id="0", mkstream=True)
    try:
        payload = detection_payload(camera, 1)
        await redis.set(payload["frame_reference"], platform_frame())
        await redis.xadd(STREAM, {"data": json.dumps(payload)})
        res = await redis.xreadgroup(c.group_name, c.consumer_name, {STREAM: ">"}, count=1)
        msg_id, fields = res[0][1][0]
        await c._handle(STREAM, msg_id, fields)

        stats = c.stats()
        async with c._session_factory() as s:
            n = (await s.execute(text(
                "SELECT count(*) FROM recognition_events WHERE camera_id = CAST(:c AS uuid)"
            ), {"c": camera})).scalar_one()
        assert n == 1, stats  # before the fix: 0 rows, rejected as too_blurry
        assert stats["precheck_too_blurry"] == 0, stats
        assert stats["rows_written"] == 1, stats
        assert stats["per_camera"][camera] == {"rows_written": 1}
    finally:
        await c._engine.dispose()
        await c._cam_config.close()
