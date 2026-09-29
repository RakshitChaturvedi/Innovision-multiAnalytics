"""Save the camera's REAL current view with a 0.0-1.0 grid, to read zone polygons from.

    python -m tools.config.snapshot --camera "Phone 1" [--out lobby.png]

The camera name is resolved via the registry's internal endpoint. The latest
entry of frames:{camera_id} gives the frame_reference (GET verbatim from Redis),
with the MinIO cold copy as fallback (shared/frames.py). The PNG shows the frame
inside a margin with grid lines every 0.1 labelled in zone coordinates: x to the
right, y downwards, (0,0) top-left, (1,1) bottom-right.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from tools.platform.common import RegistryClient, RegistryError, Reporter, load_env

MARGIN = 48
GRID = 10


def draw_grid(frame: np.ndarray, caption: str) -> np.ndarray:
    import cv2

    h, w = frame.shape[:2]
    canvas = np.full((h + 2 * MARGIN, w + 2 * MARGIN, 3), 255, np.uint8)
    canvas[MARGIN:MARGIN + h, MARGIN:MARGIN + w] = frame
    scale = max(0.4, min(w, h) / 1400)
    for i in range(GRID + 1):
        f = i / GRID
        x = MARGIN + round(f * (w - 1))
        y = MARGIN + round(f * (h - 1))
        thick = 2 if i in (0, 5, GRID) else 1
        cv2.line(canvas, (x, MARGIN), (x, MARGIN + h - 1), (0, 255, 255), thick)
        cv2.line(canvas, (MARGIN, y), (MARGIN + w - 1, y), (0, 255, 255), thick)
        label = f"{f:.1f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        cv2.putText(canvas, label, (x - tw // 2, MARGIN - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (0, 0, 0), 1, cv2.LINE_AA)                             # x, top
        cv2.putText(canvas, label, (max(2, MARGIN - tw - 6), y + th // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (0, 0, 0), 1, cv2.LINE_AA)                             # y, left
    cv2.putText(canvas, caption, (MARGIN, MARGIN + h + MARGIN - 14), cv2.FONT_HERSHEY_SIMPLEX,
                scale, (0, 0, 0), 1, cv2.LINE_AA)
    return canvas


async def latest_frame(redis, storage, camera_id: str):
    """(jpeg bytes, FrameEvent) of the newest frames:{camera_id} entry, or raise LookupError."""
    from shared.frames import fetch_frame
    from shared.platform_contracts.frame_event import FrameEvent

    entries = await redis.xrevrange(f"frames:{camera_id}", count=1)
    if not entries:
        raise LookupError(f"stream frames:{camera_id} is empty")
    _id, fields = entries[0]
    raw = fields.get(b"data") or fields.get("data")
    event = FrameEvent.model_validate_json(raw)
    data = await fetch_frame(redis, storage, camera_id=event.camera_id, frame_seq=event.frame_seq,
                             frame_reference=event.frame_reference, frame_provider=event.frame_provider)
    return data, event


async def snapshot(client: RegistryClient, redis, storage, camera: str, out: Path | None,
                   r: Reporter) -> Path | None:
    import cv2

    from shared.frames import FrameUnavailable

    try:
        cam = client.resolve(camera)
    except RegistryError as exc:
        r.fail("camera", str(exc), exc.fix)
        return None
    try:
        data, event = await latest_frame(redis, storage, cam.id)
    except LookupError as exc:
        r.fail("frame", f"{camera!r} ({cam.id}): {exc}",
               "start the platform ingestion for this camera (docs/INTEGRATION.md step 7) and "
               "check the camera URL with scripts\\test_camera_url.ps1")
        return None
    except FrameUnavailable as exc:
        r.fail("frame", f"{camera!r}: latest frame expired from Redis and not in MinIO ({exc})",
               "check MINIO_ENDPOINT/keys in .env, or retry while ingestion is running")
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        r.fail("frame", f"{camera!r}: frame {event.frame_seq} is not a decodable image",
               "retry; if it persists check the platform ingestion logs")
        return None
    age = (datetime.now(timezone.utc) - event.timestamp).total_seconds()
    h, w = img.shape[:2]
    caption = (f"{camera}  {cam.id}  seq {event.frame_seq}  "
               f"{event.timestamp.isoformat(timespec='seconds')}  {w}x{h}")
    if out is None:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", camera).strip("_") or "camera"
        out = Path(f"snapshot_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.png")
    cv2.imwrite(str(out), draw_grid(img, caption))
    r.ok("snapshot", f"{out} ({w}x{h}, frame seq {event.frame_seq}, {age:.0f}s old)")
    if age > 60:
        r.warn("frame age", f"latest frame is {age:.0f}s old: the camera may have stopped",
               "check the platform ingestion is running for this camera")
    r.info("Read zone corners off the grid: [x, y] with x to the right, y downwards, 0.0-1.0.")
    return out


async def _run(args) -> int:
    import redis.asyncio as aioredis

    from shared.config import settings
    from shared.storage.storage_minio_client import open_frame_storage

    env = load_env()
    r = Reporter()
    redis = aioredis.from_url(settings.redis_url())
    try:
        storage = await open_frame_storage()
        await snapshot(RegistryClient.from_env(env), redis, storage, args.camera,
                       Path(args.out) if args.out else None, r)
    finally:
        await redis.aclose()
    return r.summary()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", required=True, help="camera name as registered in the platform")
    ap.add_argument("--out", help="PNG path (default snapshot_<camera>_<timestamp>.png)")
    return asyncio.run(_run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
