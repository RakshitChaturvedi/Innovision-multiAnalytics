"""fetch_frame / minio_frame_key / platform FrameEvent. Runs on FAKES."""
import json
import logging
import socket
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import urllib3
from minio.error import S3Error

from shared.errors import PermanentError
from shared.frames import FrameUnavailable, fetch_frame, minio_frame_key
from shared.platform_contracts.enums import FrameProvider
from shared.platform_contracts.frame_event import FrameEvent as PlatformFrameEvent
from shared.schemas.events import DetectionEvent, FrameEvent
from shared.storage.storage_minio_client import (
    MinioSettings,
    StorageClient,
    open_frame_storage,
)


class FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}
        self.get_keys = []

    async def get(self, key):
        self.get_keys.append(key)
        return self.store.get(key)


class FakeStorage:
    """Stands in for StorageClient (sync .download, like the real one)."""

    def __init__(self, objects=None, error=None):
        self.objects = objects or {}
        self.error = error
        self.calls = []

    def download(self, bucket_key, object_key):
        self.calls.append((bucket_key, object_key))
        if self.error:
            raise self.error
        if object_key not in self.objects:
            raise s3_error("NoSuchKey")
        return self.objects[object_key]


def s3_error(code):
    return S3Error(code=code, message=code, resource="", request_id="",
                   host_id="", response=MagicMock())


CAM = uuid4()


def call(redis, storage, provider=FrameProvider.REDIS, ref="frame:cam:42", seq=42):
    return fetch_frame(redis, storage, camera_id=CAM, frame_seq=seq,
                       frame_reference=ref, frame_provider=provider)


def test_minio_frame_key_format():
    assert minio_frame_key("cam", 42) == "frames/cam/00000042.jpg"


async def test_fetch_frame_uses_reference_verbatim():
    redis = FakeRedis({"frame:cam:42": b"jpg"})
    assert await call(redis, FakeStorage()) == b"jpg"
    assert redis.get_keys == ["frame:cam:42"]
    assert not any(k.startswith("frames:") for k in redis.get_keys)


async def test_fetch_frame_falls_back_to_minio():
    redis = FakeRedis()
    storage = FakeStorage({minio_frame_key(CAM, 42): b"cold"})
    assert await call(redis, storage) == b"cold"
    assert storage.calls == [("frames", f"frames/{CAM}/00000042.jpg")]


async def test_fetch_frame_raises_when_both_miss():
    with pytest.raises(FrameUnavailable) as ei:
        await call(FakeRedis(), FakeStorage())
    assert isinstance(ei.value, PermanentError)


async def test_fetch_frame_minio_provider_skips_redis():
    redis = FakeRedis({"frame:cam:42": b"hot"})
    storage = FakeStorage({minio_frame_key(CAM, 42): b"cold"})
    got = await call(redis, storage, provider=FrameProvider.MINIO,
                     ref=minio_frame_key(CAM, 42))
    assert got == b"cold"
    assert redis.get_keys == []


async def test_fetch_frame_transient_minio_error_is_not_permanent():
    storage = FakeStorage(error=s3_error("InternalError"))
    with pytest.raises(S3Error):
        await call(FakeRedis(), storage)


async def test_fetch_frame_redis_error_propagates():
    class Boom(FakeRedis):
        async def get(self, key):
            raise ConnectionError("redis down")

    with pytest.raises(ConnectionError):
        await call(Boom(), FakeStorage())


def test_platform_frame_event_json_parses_into_pipeline():
    """Exact JSON shape the platform emits: no `profile`, no frames: prefix."""
    payload = {
        "event_id": str(uuid4()),
        "camera_id": str(CAM),
        "frame_seq": 42,
        "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "frame_provider": "redis",
        "frame_reference": f"frame:{CAM}:42",
        "frame_shape": [1080, 1920],
    }
    event = FrameEvent.model_validate_json(json.dumps(payload))
    assert FrameEvent is PlatformFrameEvent
    assert event.frame_provider == FrameProvider.REDIS
    assert event.frame_reference == f"frame:{CAM}:42"

    # detection imports ultralytics; only run this half where it is installed
    pytest.importorskip("ultralytics.trackers")
    from services.detection.src.publisher import build_detection_event

    detection = build_detection_event(event, [], 1.5)
    assert isinstance(detection, DetectionEvent)
    assert detection.frame_provider == FrameProvider.REDIS
    assert detection.frame_reference == event.frame_reference
    assert detection.frame_event_id == event.event_id


# ------------------------------------------------ MinIO missing / unreachable


class UnreachableStorage(FakeStorage):
    """Real outage bookkeeping (StorageClient.mark_*), fake transport."""

    def __init__(self):
        super().__init__(error=urllib3.exceptions.MaxRetryError(None, "/", "refused"))
        self.real = StorageClient(MinioSettings(minio_endpoint="127.0.0.1:1"))
        self.enabled = True

    def mark_unreachable(self, exc):
        self.real.mark_unreachable(exc)

    def mark_reachable(self):
        self.real.mark_reachable()


async def test_redis_miss_without_minio_is_frame_unavailable():
    with pytest.raises(FrameUnavailable):
        await call(FakeRedis(), None)


async def test_redis_miss_with_disabled_minio_is_frame_unavailable():
    disabled = StorageClient(MinioSettings(minio_endpoint=""))
    with pytest.raises(FrameUnavailable):
        await call(FakeRedis(), disabled)


async def test_unreachable_minio_is_frame_unavailable_and_logged_once(caplog):
    storage = UnreachableStorage()
    with caplog.at_level(logging.WARNING):
        for seq in range(5):
            with pytest.raises(FrameUnavailable):
                await call(FakeRedis(), storage, seq=seq)
    outage_logs = [r for r in caplog.records if "minio_unreachable" in r.message]
    assert len(outage_logs) == 1 and outage_logs[0].levelno == logging.WARNING
    assert len(storage.calls) == 5  # still tried each time: recovers by itself


async def test_outage_is_logged_again_after_recovery(caplog):
    storage = UnreachableStorage()
    with caplog.at_level(logging.INFO):
        with pytest.raises(FrameUnavailable):
            await call(FakeRedis(), storage)
        storage.error = None
        storage.objects = {minio_frame_key(CAM, 42): b"cold"}
        assert await call(FakeRedis(), storage) == b"cold"
        storage.error = urllib3.exceptions.MaxRetryError(None, "/", "refused")
        with pytest.raises(FrameUnavailable):
            await call(FakeRedis(), storage)
    messages = [r.message for r in caplog.records]
    assert sum("minio_unreachable" in m for m in messages) == 2
    assert sum("minio_reachable_again" in m for m in messages) == 1


async def test_real_client_on_closed_port_is_frame_unavailable_fast(caplog):
    """A real StorageClient against a port nothing listens on (refused)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again when the block ends
    storage = StorageClient(MinioSettings(minio_endpoint=f"127.0.0.1:{port}", minio_timeout_s=1))
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        for seq in range(3):
            with pytest.raises(FrameUnavailable):
                await call(FakeRedis(), storage, seq=seq)
    assert time.monotonic() - started < 10
    assert sum("minio_unreachable" in r.message for r in caplog.records) == 1


async def test_open_frame_storage_not_configured_logs_once_returns_none(caplog):
    with caplog.at_level(logging.WARNING):
        assert await open_frame_storage(MinioSettings(minio_endpoint="")) is None
    assert sum("minio_not_configured" in r.message for r in caplog.records) == 1


async def test_open_frame_storage_unreachable_returns_client_and_logs(caplog):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with caplog.at_level(logging.WARNING):
        storage = await open_frame_storage(
            MinioSettings(minio_endpoint=f"127.0.0.1:{port}", minio_timeout_s=1)
        )
    assert storage is not None and storage.enabled
    assert sum("minio_unreachable" in r.message for r in caplog.records) == 1
    # a later frame during the same outage does not log again
    with caplog.at_level(logging.WARNING):
        with pytest.raises(FrameUnavailable):
            await call(FakeRedis(), storage)
    assert sum("minio_unreachable" in r.message for r in caplog.records) == 1
