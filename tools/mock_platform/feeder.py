"""Mock platform ingestion: publish frames exactly like the platform does.

    python -m tools.mock_platform.feeder --video demo.mp4 --camera-id <uuid> [--fps 10] [--loop]
    python -m tools.mock_platform.feeder --images frames_dir/ --camera-id <uuid>

Per frame: SET frame:{cam}:{seq} (TTL 20 s), cold copy to MinIO
(innovision-frames, shared.frames.minio_frame_key) when MINIO_ENDPOINT is set,
then XADD frames:{cam} {"data": <platform FrameEvent JSON>}.
--inject-bad N additionally publishes N invalid payloads (they must end up in
frames:{cam}:dlq, nothing else).
"""
import argparse
import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from shared.frames import FRAMES_BUCKET_KEY, minio_frame_key
from shared.platform_contracts.enums import FrameProvider
from shared.platform_contracts.frame_event import FrameEvent

logger = logging.getLogger(__name__)

FRAME_TTL_S = 20
FRAMES_MAXLEN = 1000


def frame_key(camera_id: UUID, frame_seq: int) -> str:
    return f"frame:{camera_id}:{frame_seq}"


def frames_stream(camera_id: UUID) -> str:
    return f"frames:{camera_id}"


def build_frame_event(
    camera_id: UUID, frame_seq: int, timestamp: datetime, shape: tuple[int, int]
) -> FrameEvent:
    return FrameEvent(
        camera_id=camera_id,
        frame_seq=frame_seq,
        timestamp=timestamp,
        frame_provider=FrameProvider.REDIS,
        frame_reference=frame_key(camera_id, frame_seq),
        frame_shape=shape,
    )


async def publish_frame(
    redis,
    storage,
    *,
    camera_id: UUID,
    frame_seq: int,
    timestamp: datetime,
    jpeg: bytes,
    shape: tuple[int, int],
    ttl_s: int = FRAME_TTL_S,
) -> FrameEvent:
    """Frame bytes first (hot + cold), then the event: a consumer never sees an
    event whose frame was not written."""
    event = build_frame_event(camera_id, frame_seq, timestamp, shape)
    await redis.set(event.frame_reference, jpeg, ex=ttl_s)
    if storage is not None:
        await asyncio.to_thread(
            storage.upload, FRAMES_BUCKET_KEY, minio_frame_key(camera_id, frame_seq),
            jpeg, "image/jpeg",
        )
    await redis.xadd(
        frames_stream(camera_id), {"data": event.model_dump_json()},
        maxlen=FRAMES_MAXLEN, approximate=True,
    )
    return event


async def publish_bad(redis, camera_id: UUID, n: int) -> None:
    """Deliberately invalid messages (for the DLQ path)."""
    for i in range(n):
        await redis.xadd(frames_stream(camera_id), {"data": f'{{"bad": {i}}}'})


def iter_frames(video: str | None, images: str | None, loop: bool) -> Iterator[tuple[bytes, tuple[int, int]]]:
    import cv2

    while True:
        if images:
            for p in sorted(Path(images).glob("*.jp*g")):
                img = cv2.imread(str(p))
                if img is None:
                    logger.warning("feeder_unreadable_image path=%s", p)
                    continue
                yield p.read_bytes(), img.shape[:2]
        else:
            cap = cv2.VideoCapture(video)
            if not cap.isOpened():
                raise SystemExit(f"cannot open video {video}")
            try:
                while True:
                    ok, img = cap.read()
                    if not ok:
                        break
                    ok, buf = cv2.imencode(".jpg", img)
                    if ok:
                        yield buf.tobytes(), img.shape[:2]
            finally:
                cap.release()
        if not loop:
            return


async def run(args) -> int:
    import redis.asyncio as aioredis

    from shared.config import settings
    from shared.storage.storage_minio_client import open_frame_storage

    redis = aioredis.from_url(args.redis_url or settings.redis_url())
    storage = await open_frame_storage()
    cams = [UUID(c) for c in args.camera_id]
    period = 1.0 / args.fps
    ts = datetime.now(timezone.utc)
    seq = args.start_seq
    try:
        for c in cams:
            await publish_bad(redis, c, args.inject_bad)
        frames = await asyncio.to_thread(lambda: iter_frames(args.video, args.images, args.loop))
        while True:
            item = await asyncio.to_thread(next, frames, None)
            if item is None:
                break
            jpeg, shape = item
            for c in cams:
                await publish_frame(
                    redis, storage, camera_id=c, frame_seq=seq, timestamp=ts,
                    jpeg=jpeg, shape=shape,
                )
            seq += 1
            ts += timedelta(seconds=period)
            if args.limit and seq - args.start_seq >= args.limit:
                break
            await asyncio.sleep(period)
    finally:
        await redis.aclose()
    logger.info("feeder_done frames=%d cameras=%d", seq - args.start_seq, len(cams))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video")
    src.add_argument("--images", help="directory of .jpg frames")
    ap.add_argument("--camera-id", action="append", required=True, help="repeat for several cameras")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N frames (0 = no limit)")
    ap.add_argument("--start-seq", type=int, default=0,
                    help="first frame_seq; use a higher one after a restart")
    ap.add_argument("--inject-bad", type=int, default=0)
    ap.add_argument("--redis-url", help="default: REDIS_HOST/PORT/DB from env")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
