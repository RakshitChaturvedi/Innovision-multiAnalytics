"""
Face-region estimation.

This is NOT a face detection model.

It estimates the likely face region from the top portion
of a person bounding box.

Purpose:

    Avoid sending every person crop to the recognition
    worker when the face is too small or not visible.

No CameraProfile is used here.
"""

from dataclasses import dataclass

# =============================================================
# FACE ESTIMATE
# =============================================================

@dataclass
class FaceEstimate:
    """
    Normalized face-region estimate.

    Coordinates are in the range:

        0.0 -> 1.0
    """

    x1: float
    y1: float
    x2: float
    y2: float

    has_face: bool


# =============================================================
# FACE ESTIMATOR
# =============================================================

class FaceEstimator:
    """
    Estimates the face region using the top portion
    of the person's bounding box.
    """

    def __init__(
        self,
        face_region_ratio: float = 0.35,
        face_area_min: float = 0.001,
    ) -> None:

        self.face_region_ratio = (
            face_region_ratio
        )

        self.face_area_min = (
            face_area_min
        )

    # =========================================================
    # ESTIMATE
    # =========================================================

    def estimate(
        self,
        x1_norm: float,
        y1_norm: float,
        x2_norm: float,
        y2_norm: float,
    ) -> FaceEstimate:
        """
        Estimate the face region from a normalized
        person bounding box.
        """

        # -----------------------------------------------------
        # Person bounding-box height
        # -----------------------------------------------------

        bbox_height = (
            y2_norm - y1_norm
        )

        # -----------------------------------------------------
        # Assume face is in the top portion
        # -----------------------------------------------------

        face_bottom = (
            y1_norm
            + bbox_height
            * self.face_region_ratio
        )

        face_x1 = x1_norm
        face_y1 = y1_norm

        face_x2 = x2_norm
        face_y2 = face_bottom

        # -----------------------------------------------------
        # Calculate estimated face area
        # -----------------------------------------------------

        face_width = (
            face_x2 - face_x1
        )

        face_height = (
            face_y2 - face_y1
        )

        area = (
            face_width
            * face_height
        )

        has_face = (
            area > self.face_area_min
        )

        return FaceEstimate(
            x1=face_x1,
            y1=face_y1,
            x2=face_x2,
            y2=face_y2,
            has_face=has_face,
        )