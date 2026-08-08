"""
Post-tracking detection filtering.

Responsibilities:

    1. Remove low-confidence tracks.
    2. Normalize bounding-box coordinates.
    3. Remove invalid bounding boxes.
    4. Estimate face region.
    5. Build FilteredTrack objects.

CameraProfile is not used.
"""

from dataclasses import dataclass

from shared.schemas.common import BoundingBox

from .config import settings
from .face_estimator import FaceEstimator
from .tracker import TrackedDetection


# =============================================================
# FILTERED TRACK
# =============================================================

@dataclass
class FilteredTrack:
    """
    Fully processed track.

    Coordinates are normalized between 0 and 1.
    """

    track_id: int

    bbox: BoundingBox

    confidence: float

    class_label: str

    has_face: bool

    face_bbox: BoundingBox | None


# =============================================================
# DETECTION FILTER
# =============================================================

class DetectionFilter:
    """
    Filters and normalizes tracked detections.
    """

    def __init__(
        self,
        face_estimator: FaceEstimator,
        min_confidence: float = 0.5,
    ) -> None:

        self._face_estimator = (
            face_estimator
        )

        self._min_confidence = (
            min_confidence
        )

    # =========================================================
    # APPLY
    # =========================================================

    def apply(
        self,
        tracked: list[TrackedDetection],
        frame_width: int,
        frame_height: int,
    ) -> list[FilteredTrack]:
        """
        Filter and normalize tracked detections.
        """

        result: list[
            FilteredTrack
        ] = []

        width = frame_width
        height = frame_height

        if width <= 0 or height <= 0:
            return result

        for track in tracked:

            # -------------------------------------------------
            # Confidence filtering
            # -------------------------------------------------

            if (
                track.confidence
                < self._min_confidence
            ):
                continue

            # -------------------------------------------------
            # Pixel -> normalized coordinates
            # -------------------------------------------------

            x1 = max(
                0.0,
                min(
                    track.x1 / width,
                    1.0,
                ),
            )

            y1 = max(
                0.0,
                min(
                    track.y1 / height,
                    1.0,
                ),
            )

            x2 = max(
                0.0,
                min(
                    track.x2 / width,
                    1.0,
                ),
            )

            y2 = max(
                0.0,
                min(
                    track.y2 / height,
                    1.0,
                ),
            )

            # -------------------------------------------------
            # Invalid / degenerate bounding box
            # -------------------------------------------------

            if x2 <= x1 or y2 <= y1:
                continue

            bbox = BoundingBox(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
            )

            # -------------------------------------------------
            # Face estimation
            # -------------------------------------------------

            face = (
                self._face_estimator.estimate(
                    x1,
                    y1,
                    x2,
                    y2,
                )
            )

            face_bbox = None

            if face.has_face:

                face_bbox = BoundingBox(
                    x1=face.x1,
                    y1=face.y1,
                    x2=face.x2,
                    y2=face.y2,
                )

            # -------------------------------------------------
            # Build filtered track
            # -------------------------------------------------

            result.append(
                FilteredTrack(
                    track_id=track.track_id,
                    bbox=bbox,
                    confidence=track.confidence,

                    # YOLO is restricted to class 0,
                    # therefore every detection is a person.
                    class_label="person",

                    has_face=face.has_face,

                    face_bbox=face_bbox,
                )
            )

        return result