"""
Crops face region from full frame using normalized face_bbox.
Handles boundary clamping.
"""
import logging

import numpy as np

from shared.schemas.common import BoundingBox

logger = logging.getLogger(__name__)


class FaceCropper:
    def crop(self, frame: np.ndarray, face_bbox: BoundingBox) -> np.ndarray | None:
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

        return crop
