"""
Detection event publisher.

All cameras use the same detection pipeline.

There is no CameraProfile and therefore no
CROWD_ONLY routing.

Every processed frame produces a DetectionEvent
when detections are available.
"""

import logging

import redis.asyncio as aioredis

from shared.schemas.common import TrackResult
# FIX: shared.schemas.contracts does not exist; the module is events.
from shared.schemas.events import DetectionEvent, FrameEvent

from .config import settings
from .filtering import FilteredTrack


logger = logging.getLogger(__name__)


# =============================================================
# TRACK RESULT
# =============================================================

def build_track_result(
    track: FilteredTrack,
) -> TrackResult:
    """
    Convert internal FilteredTrack into the
    shared TrackResult schema.
    """

    return TrackResult(
        track_id=track.track_id,

        bbox=track.bbox,

        confidence=track.confidence,

        class_label=track.class_label,

        has_face=track.has_face,

        face_bbox=track.face_bbox,
    )


# =============================================================
# DETECTION EVENT
# =============================================================

def build_detection_event(
    frame_event: FrameEvent,
    tracks: list[FilteredTrack],
    inference_latency_ms: float,
) -> DetectionEvent:
    """
    Construct the public DetectionEvent.

    CameraProfile is intentionally NOT included.
    """

    # FIX: the previous call passed `event_type` and `track_id`, which
    # are not fields on DetectionEvent (pydantic silently dropped them),
    # while omitting `frame_shape` and `profile`, which ARE required.
    # Every publish therefore raised ValidationError.
    return DetectionEvent(
        camera_id=frame_event.camera_id,

        frame_event_id=(
            frame_event.event_id
        ),

        timestamp=frame_event.timestamp,

        frame_reference=(
            frame_event.frame_reference
        ),

        frame_seq=(
            frame_event.frame_seq
        ),

        frame_shape=(
            frame_event.frame_shape
        ),

        profile=frame_event.profile,

        tracks=[
            build_track_result(track)
            for track in tracks
        ],

        inference_latency_ms=(
            inference_latency_ms
        ),
    )


# =============================================================
# PUBLISHER
# =============================================================

class DetectionPublisher:
    """
    Publishes DetectionEvents to Redis Streams.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
    ) -> None:

        self._redis = redis_client

    # =========================================================
    # PUBLISH
    # =========================================================

    async def publish(
        self,
        frame_event: FrameEvent,
        tracks: list[FilteredTrack],
        inference_latency_ms: float,
    ) -> None:

        event = build_detection_event(
            frame_event=frame_event,
            tracks=tracks,
            inference_latency_ms=(
                inference_latency_ms
            ),
        )

        # Route per-camera so downstream consumers can shard, and keep
        # a fan-in stream for services that want everything.
        await self._redis.xadd(
            settings.detections_stream,
            {
                "data": (
                    event.model_dump_json()
                )
            },
            maxlen=settings.detections_maxlen,
            approximate=True,
        )

        logger.debug(
            "detection_published "
            "camera_id=%s "
            "tracks=%d "
            "latency_ms=%.1f",
            frame_event.camera_id,
            len(tracks),
            inference_latency_ms,
        )