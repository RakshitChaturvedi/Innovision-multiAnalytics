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
from shared.schemas.contracts import FrameEvent
from shared.schemas.events import DetectionEvent

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

    return DetectionEvent(
        event_type="detection",

        camera_id=frame_event.camera_id,

        timestamp=frame_event.timestamp,

        track_id=None,

        frame_reference=(
            frame_event.frame_reference
        ),

        frame_provider=(
            frame_event.frame_provider
        ),

        tracks=[
            build_track_result(track)
            for track in tracks
        ],

        inference_latency_ms=(
            inference_latency_ms
        ),

        frame_event_id=(
            frame_event.event_id
        ),

        frame_seq=(
            frame_event.frame_seq
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