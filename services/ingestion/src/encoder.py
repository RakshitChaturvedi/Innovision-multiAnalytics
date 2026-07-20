from __future__ import annotations

import cv2
import numpy as np

class JPEGEncoder:
    # opencv (bgr) frames -> jpeg format

    def __init__(self, quality: int = 85) -> None:
        self._encode_params = [cv2.IMWRITE_JPEG_QUALITY, quality]

    def encode(self, frame: np.ndarray) -> bytes:
        success, encoded = cv2.imencode(".jpg", frame, self._encode_params)
        if not success:
            raise RuntimeError("Failed to encode frame as JPEG")
        return encoded.tobytes()