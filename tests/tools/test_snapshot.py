"""tools/config/snapshot.py: latest frame from REAL Redis (db 15), MinIO fallback, grid PNG."""
import uuid
from datetime import datetime, timezone

import cv2
import numpy as np

from shared.platform_contracts.enums import FrameProvider
from shared.platform_contracts.frame_event import FrameEvent
from tools.config import snapshot as snap
from tools.platform.common import Reporter

W, H = 320, 180


def _jpeg(value):
    ok, buf = cv2.imencode(".jpg", np.full((H, W, 3), value, np.uint8))
    return buf.tobytes()


async def _publish(redis, cam_id, seq, value, store_frame=True):
    ev = FrameEvent(camera_id=cam_id, frame_seq=seq, timestamp=datetime.now(timezone.utc),
                    frame_provider=FrameProvider.REDIS, frame_reference=f"frame:{cam_id}:{seq}",
                    frame_shape=(H, W))
    if store_frame:
        await redis.set(ev.frame_reference, _jpeg(value), ex=20)
    await redis.xadd(f"frames:{cam_id}", {"data": ev.model_dump_json()})


class FakeMinio:
    enabled = True

    def __init__(self, objects):
        self.objects = objects
        self.keys = []

    def download(self, bucket, key):
        self.keys.append((bucket, key))
        return self.objects[key]


async def test_snapshot_uses_latest_frame_and_draws_grid(redis_client, registry, tmp_path, capsys):
    cam = registry.add("Phone 1")
    await _publish(redis_client, cam["id"], 1, 20)
    await _publish(redis_client, cam["id"], 2, 200)   # newest
    out = tmp_path / "s.png"
    r = Reporter()
    assert await snap.snapshot(registry.client(), redis_client, None, "Phone 1", out, r) == out
    assert r.failed == 0
    img = cv2.imread(str(out))
    m = snap.MARGIN
    assert img.shape[:2] == (H + 2 * m, W + 2 * m)
    # a cell centre carries the newest frame (value ~200), not the older one
    assert abs(int(img[m + 9, m + 16].mean()) - 200) < 10
    # grid line at x=0.5 is yellow (BGR 0,255,255)
    x = m + round(0.5 * (W - 1))
    assert tuple(int(v) for v in img[m + H // 3, x]) == (0, 255, 255)


async def test_snapshot_falls_back_to_minio_key(redis_client, registry, tmp_path):
    cam = registry.add("Phone 1")
    await _publish(redis_client, cam["id"], 7, 90, store_frame=False)   # expired from Redis
    minio = FakeMinio({f"frames/{cam['id']}/00000007.jpg": _jpeg(90)})
    r = Reporter()
    assert await snap.snapshot(registry.client(), redis_client, minio, "Phone 1", tmp_path / "s.png", r)
    assert minio.keys == [("frames", f"frames/{cam['id']}/00000007.jpg")]


async def test_empty_stream_fails_with_fix(redis_client, registry, tmp_path, capsys):
    registry.add("Phone 1")
    r = Reporter()
    assert await snap.snapshot(registry.client(), redis_client, None, "Phone 1", tmp_path / "s.png", r) is None
    out = capsys.readouterr().out
    assert r.failed == 1 and "ingestion" in out


async def test_unknown_camera_lists_names(redis_client, registry, tmp_path, capsys):
    registry.add("Phone 1")
    r = Reporter()
    await snap.snapshot(registry.client(), redis_client, None, "Phone 7", tmp_path / "s.png", r)
    assert r.failed == 1 and "'Phone 1'" in capsys.readouterr().out


async def test_expired_frame_without_minio_fails(redis_client, registry, tmp_path, capsys):
    cam = registry.add("Phone 1")
    await _publish(redis_client, cam["id"], 3, 50, store_frame=False)
    r = Reporter()
    await snap.snapshot(registry.client(), redis_client, None, "Phone 1", tmp_path / "s.png", r)
    assert r.failed == 1 and "MinIO" in capsys.readouterr().out
