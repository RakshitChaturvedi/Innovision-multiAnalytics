"""
Per-camera ByteTrack tracking.

Important:

CameraProfile has been completely removed.

Tracking is still maintained separately for each camera
because track IDs must persist independently per stream.
"""

import logging

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

from ultralytics.trackers import BYTETracker

from .config import settings
from .detector import RawDetection


logger = logging.getLogger(__name__)


_BYTETRACK_ARGS = SimpleNamespace(
    track_high_thresh=0.5,
    track_low_thresh=0.1,
    new_track_thresh=0.6,
    track_buffer=30,
    match_thresh=0.8,
    fuse_score=True,
)


@dataclass
class TrackedDetection:
    """
    Detection enriched with a persistent track ID.
    """

    track_id: int

    x1: float
    y1: float
    x2: float
    y2: float

    confidence: float

    class_id: int


class CameraTracker:
    """
    ByteTrack wrapper for one camera.

    Each camera gets its own instance.

    Example:

        camera-A -> CameraTracker A
        camera-B -> CameraTracker B
        camera-C -> CameraTracker C

    They must not share tracker state.
    """

    def __init__(
        self,
        camera_id: str,
    ) -> None:

        self.camera_id = camera_id

        self._tracker = self._create()

        logger.info(
            "camera_tracker_created camera_id=%s",
            camera_id,
        ) 

    def update(
        self,
        detections: list[RawDetection],
        frame: np.ndarray,
    ) -> list[TrackedDetection]:
        """
        Update ByteTrack with detections from one frame.
        """

        if not detections:

            empty = np.zeros(
                (0, 6),
                dtype=np.float32,
            )

            self._tracker.update(
                empty,
                frame,
            )

            return []


        detection_array = np.array(
            [
                [
                    detection.x1,
                    detection.y1,
                    detection.x2,
                    detection.y2,
                    detection.confidence,
                    detection.class_id,
                ]
                for detection in detections
            ],
            dtype=np.float32,
        )

        tracks = self._tracker.update(
            detection_array,
            frame,
        )

        if tracks is None:
            return []

        if len(tracks) == 0:
            return []

        tracked: list[
            TrackedDetection
        ] = []

        for track in tracks:

            if len(track) < 5:
                continue

            x1 = float(track[0])
            y1 = float(track[1])
            x2 = float(track[2])
            y2 = float(track[3])

            track_id = int(track[4])

            if len(track) > 5:
                confidence = float(track[5])
            else:
                confidence = (
                    settings.detection_confidence
                )

            if len(track) > 6:
                class_id = int(track[6])
            else:
                class_id = 0

            tracked.append(
                TrackedDetection(
                    track_id=track_id,
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    confidence=confidence,
                    class_id=class_id,
                )
            )

        return tracked


    def reset(self) -> None:
        """
        Reset tracker state.

        Useful after a long camera stream interruption.
        """

        self._tracker = self._create()

        logger.info(
            "camera_tracker_reset camera_id=%s",
            self.camera_id,
        )

    def switch_to_botsort(self) -> None:
        """
        Placeholder for future BoT-SORT support.

        CameraProfile is NOT involved.

        A camera shake event can trigger this in a future
        implementation.
        """

        logger.warning(
            "tracker_switch_botsort_requested "
            "camera_id=%s "
            "feature_not_implemented",
            self.camera_id,
        )


    @staticmethod
    def _create() -> BYTETracker:

        return BYTETracker(
            _BYTETRACK_ARGS,
            frame_rate=10,
        )


class TrackerManager:
    """
    Registry of CameraTracker instances.

    One tracker per camera.

    There is NO CameraProfile.
    """

    def __init__(self) -> None:

        self._trackers: dict[
            str,
            CameraTracker,
        ] = {}

    def get(
        self,
        camera_id: str,
    ) -> CameraTracker:

        if camera_id not in self._trackers:

            self._trackers[camera_id] = (
                CameraTracker(
                    camera_id
                )
            )

        return self._trackers[camera_id]
    def reset(
        self,
        camera_id: str,
    ) -> None:

        tracker = self._trackers.get(
            camera_id
        )

        if tracker is not None:

            tracker.reset()
    def on_shake(
        self,
        camera_id: str,
    ) -> None:

        tracker = self._trackers.get(
            camera_id
        )

        if tracker is not None:

            tracker.switch_to_botsort()