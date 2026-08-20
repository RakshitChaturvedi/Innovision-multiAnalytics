"""
Crops face region from full frame using normalized face_bbox.
Handles boundary clamping.
"""
import logging
from dataclasses import dataclass

import numpy as np

from shared.schemas.common import BoundingBox

logger = logging.getLogger(__name__)


@dataclass
class CropResult:
    """
    The cropped face region, plus the frame-pixel offset it was cut from.

    That offset is what lets a crop-local box (e.g. SCRFD's face.bbox,
    which is in the crop's own pixel space starting at (0,0)) be mapped
    back onto the original frame later: frame_x = x1 + crop_local_x.
    Without carrying x1/y1 forward, that mapping isn't possible once
    crop() returns — this exists specifically so consumer.py can do that
    transform after the model runs.
    """
    image: np.ndarray
    x1: int
    y1: int


class FaceCropper:
    def crop(self, frame: np.ndarray, face_bbox: BoundingBox) -> CropResult | None:
        """
        Crops face region from a BGR frame using normalized (0-1) bbox
        coordinates. Returns None if the resulting crop would be
        degenerate or too small to be useful.
        """
        h, w = frame.shape[:2]

        x1 = int(face_bbox.x1 * w)
        y1 = int(face_bbox.y1 * h)
        x2 = int(face_bbox.x2 * w)
        y2 = int(face_bbox.y2 * h)

        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))

        if (x2 - x1) < 10 or (y2 - y1) < 10:
            logger.debug("face_crop_too_small w=%d h=%d", x2 - x1, y2 - y1)
            return None

        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None

        return CropResult(image=crop, x1=x1, y1=y1)