"""Frame retrieval for platform FrameEvents.

`frame_reference` is opaque: for the redis provider it is the exact Redis key
and is used verbatim. The cold copy lives in MinIO under `minio_frame_key`.
"""

import asyncio
import logging

from minio.error import S3Error

from shared.errors import PermanentError
from shared.platform_contracts.enums import FrameProvider

logger = logging.getLogger(__name__)

FRAMES_BUCKET_KEY = "frames"  # StorageClient bucket key -> innovision-frames

_MISSING_CODES = {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}


class FrameUnavailable(PermanentError):
    """The frame is neither in Redis nor in MinIO (expired or never written)."""


def minio_frame_key(camera_id, frame_seq: int) -> str:
    return f"frames/{camera_id}/{frame_seq:08d}.jpg"


async def _fetch_from_minio(minio_client, camera_id, frame_seq: int) -> bytes | None:
    if minio_client is None:
        return None
    key = minio_frame_key(camera_id, frame_seq)
    try:
        data = await asyncio.to_thread(minio_client.download, FRAMES_BUCKET_KEY, key)
    except S3Error as exc:
        if exc.code in _MISSING_CODES:
            return None
        raise  # anything else is transient: let the message be retried
    return data or None


async def fetch_frame(
    redis,
    minio_client,
    *,
    camera_id,
    frame_seq: int,
    frame_reference: str,
    frame_provider: FrameProvider,
) -> bytes:
    """Return the JPEG bytes of a frame.

    Redis/MinIO connection errors propagate (transient). Only "frame is gone"
    is permanent and raises FrameUnavailable.
    """
    if frame_provider == FrameProvider.REDIS:
        data = await redis.get(frame_reference)
        if data:
            return data
        logger.warning(
            "frame_redis_miss camera=%s seq=%s key=%s, trying minio",
            camera_id, frame_seq, frame_reference,
        )

    data = await _fetch_from_minio(minio_client, camera_id, frame_seq)
    if data:
        return data

    raise FrameUnavailable(
        f"frame not available camera={camera_id} seq={frame_seq} "
        f"provider={frame_provider.value} reference={frame_reference}"
    )
