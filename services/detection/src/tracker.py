"""
Per-camera ByteTrack tracking.

Tracking is maintained separately for each camera because track IDs
must persist independently per stream.

FIX
---
`BYTETracker.update()` does NOT accept a raw (N, 6) numpy array. The
ultralytics implementation calls `results.conf`, `results.xywh`,
`results.cls` and boolean-mask-indexes `results[mask]`. Passing an
ndarray raised `AttributeError: 'numpy.ndarray' object has no
attribute 'conf'` on the very first frame that contained a detection,
so tracking never worked at all.

`_DetectionResults` below is a minimal Results-like adapter that
satisfies that contract.
"""

import logging

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

from ultralytics.trackers import BOTSORT, BYTETracker

from .config import settings
from .detector import RawDetection


logger = logging.getLogger(__name__)


def _bytetrack_args() -> SimpleNamespace:
    return SimpleNamespace(
        track_high_thresh=settings.track_high_thresh,
        track_low_thresh=settings.track_low_thresh,
        new_track_thresh=settings.new_track_thresh,
        track_buffer=settings.track_buffer,
        match_thresh=settings.match_thresh,
        fuse_score=True,
        # Consumed by BOTSORT only, but harmless on BYTETracker and
        # required if the tracker is switched at runtime.
        gmc_method="sparseOptFlow",
        proximity_thresh=0.5,
        appearance_thresh=0.25,
        with_reid=False,
        model="auto",
    )


# =============================================================
# RESULTS ADAPTER
# =============================================================

class _DetectionResults:
    """
    Minimal stand-in for an ultralytics Results/Boxes object.

    Exposes exactly what BYTETracker/BOTSORT touch:
        .conf   (N,)     confidence scores
        .xyxy   (N, 4)   corner boxes
        .xywh   (N, 4)   centre boxes
        .cls    (N,)     class ids
        len(r)           detection count
        r[mask]          boolean-mask selection
    """

    __slots__ = ("xyxy", "conf", "cls")

    def __init__(
        self,
        xyxy: np.ndarray,
        conf: np.ndarray,
        cls: np.ndarray,
    ) -> None:
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls

    @classmethod
    def from_detections(
        cls,
        detections: list[RawDetection],
    ) -> "_DetectionResults":

        if not detections:
            return cls(
                np.zeros((0, 4), dtype=np.float32),
                np.zeros((0,), dtype=np.float32),
                np.zeros((0,), dtype=np.float32),
            )

        xyxy = np.array(
            [[d.x1, d.y1, d.x2, d.y2] for d in detections],
            dtype=np.float32,
        )
        conf = np.array(
            [d.confidence for d in detections],
            dtype=np.float32,
        )
        klass = np.array(
            [d.class_id for d in detections],
            dtype=np.float32,
        )
        return cls(xyxy, conf, klass)

    @property
    def xywh(self) -> np.ndarray:
        if len(self.xyxy) == 0:
            return np.zeros((0, 4), dtype=np.float32)

        x1, y1, x2, y2 = (
            self.xyxy[:, 0],
            self.xyxy[:, 1],
            self.xyxy[:, 2],
            self.xyxy[:, 3],
        )
        return np.stack(
            [
                (x1 + x2) / 2.0,
                (y1 + y2) / 2.0,
                x2 - x1,
                y2 - y1,
            ],
            axis=-1,
        ).astype(np.float32)

    def __len__(self) -> int:
        return int(self.conf.shape[0])

    def __getitem__(self, index) -> "_DetectionResults":
        return _DetectionResults(
            self.xyxy[index],
            self.conf[index],
            self.cls[index],
        )


# =============================================================
# TRACKED DETECTION
# =============================================================

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


# =============================================================
# CAMERA TRACKER
# =============================================================

class CameraTracker:
    """
    ByteTrack (or BoT-SORT) wrapper for one camera.
    """

    def __init__(
        self,
        camera_id: str,
        frame_rate: int | None = None,
    ) -> None:

        self.camera_id = camera_id

        self.frame_rate = (
            frame_rate
            if frame_rate is not None
            else settings.tracker_frame_rate
        )

        self._algorithm = "bytetrack"
        self._tracker = self._create()

        logger.info(
            "camera_tracker_created camera_id=%s algorithm=%s fps=%d",
            camera_id,
            self._algorithm,
            self.frame_rate,
        )

    # ---------------------------------------------------------

    def update(
        self,
        detections: list[RawDetection],
        frame: np.ndarray,
    ) -> list[TrackedDetection]:
        """
        Update the tracker with detections from one frame.

        Empty frames are still fed to the tracker so that Kalman
        prediction and track ageing stay in sync with wall time.
        """

        results = _DetectionResults.from_detections(detections)

        try:
            tracks = self._tracker.update(results, frame)

        except Exception as exc:
            # A tracker blow-up must not take the whole worker down.
            logger.exception(
                "tracker_update_failed camera_id=%s detections=%d error=%s",
                self.camera_id,
                len(detections),
                exc,
            )
            self.reset()
            return []

        if tracks is None or len(tracks) == 0:
            return []

        tracked: list[TrackedDetection] = []

        for track in tracks:

            if len(track) < 5:
                continue

            confidence = (
                float(track[5])
                if len(track) > 5
                else settings.detection_confidence
            )

            class_id = (
                int(track[6])
                if len(track) > 6
                else 0
            )

            tracked.append(
                TrackedDetection(
                    track_id=int(track[4]),
                    x1=float(track[0]),
                    y1=float(track[1]),
                    x2=float(track[2]),
                    y2=float(track[3]),
                    confidence=confidence,
                    class_id=class_id,
                )
            )

        return tracked

    # ---------------------------------------------------------

    def reset(self) -> None:
        """
        Reset tracker state, e.g. after a stream interruption.
        """

        self._tracker = self._create()

        logger.info(
            "camera_tracker_reset camera_id=%s",
            self.camera_id,
        )

    # ---------------------------------------------------------

    def switch_to_botsort(self) -> None:
        """
        Swap ByteTrack for BoT-SORT.

        BoT-SORT applies global motion compensation, which recovers
        track identities after a camera shake far better than plain
        ByteTrack. Existing track IDs cannot be carried across, so
        this is a hard reset of tracker state.
        """

        if self._algorithm == "botsort":
            # Already switched; just re-seed after the shake.
            self.reset()
            return

        self._algorithm = "botsort"
        self._tracker = self._create()

        logger.warning(
            "tracker_switched camera_id=%s algorithm=botsort",
            self.camera_id,
        )

    # ---------------------------------------------------------

    def _create(self):

        args = _bytetrack_args()

        if self._algorithm == "botsort":
            return BOTSORT(args, frame_rate=self.frame_rate)

        return BYTETracker(args, frame_rate=self.frame_rate)


# =============================================================
# TRACKER MANAGER
# =============================================================

class TrackerManager:
    """
    Registry of CameraTracker instances, one per camera.
    """

    def __init__(self) -> None:

        self._trackers: dict[str, CameraTracker] = {}

    def get(self, camera_id: str) -> CameraTracker:

        tracker = self._trackers.get(camera_id)

        if tracker is None:
            tracker = CameraTracker(camera_id)
            self._trackers[camera_id] = tracker

        return tracker

    def reset(self, camera_id: str) -> None:

        tracker = self._trackers.get(camera_id)

        if tracker is not None:
            tracker.reset()

    def on_shake(self, camera_id: str) -> None:

        tracker = self._trackers.get(camera_id)

        if tracker is not None:
            tracker.switch_to_botsort()

    def drop(self, camera_id: str) -> None:
        """
        Release tracker state for a camera that is no longer streaming.

        Without this, `_trackers` grows unbounded in a long-running
        worker that sees churn in the camera fleet.
        """

        self._trackers.pop(camera_id, None)

    def camera_ids(self) -> list[str]:
        return list(self._trackers)
