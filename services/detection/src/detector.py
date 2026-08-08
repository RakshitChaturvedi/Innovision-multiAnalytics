"""
Pure YOLOv11m detection.

Responsibilities:

    Frame batch
        ↓
    YOLOv11m
        ↓
    RawDetection objects

This module does NOT perform:

    - tracking
    - normalization
    - face estimation
    - database writes
    - Redis publishing

YOLOv11m is used for every camera.
"""

import logging
import time

from dataclasses import dataclass

import numpy as np

from ultralytics import YOLO

from .config import settings


logger = logging.getLogger(__name__)


# =============================================================
# RAW DETECTION
# =============================================================

@dataclass
class RawDetection:
    """
    Raw detection returned by YOLO.

    Coordinates are pixel coordinates.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    confidence: float

    class_id: int


# =============================================================
# YOLO DETECTOR
# =============================================================

class YOLODetector:
    """
    Stateless YOLOv11m inference wrapper.

    Receives a batch of frames and returns
    detections for every frame.
    """

    def __init__(self, model: YOLO) -> None:

        self._model = model

    # =========================================================
    # BATCH INFERENCE
    # =========================================================

    def run_batch(
        self,
        frames: list[np.ndarray],
    ) -> tuple[
        list[list[RawDetection]],
        float,
    ]:
        """
        Run YOLOv11m on a batch of frames.

        Parameters
        ----------
        frames:
            List of BGR numpy arrays.

        Returns
        -------
        detections_per_frame:
            Detection list corresponding to each frame.

        latency_ms:
            Total YOLO inference latency.
        """

        if not frames:
            return [], 0.0

        start_time = time.monotonic()

        # -----------------------------------------------------
        # YOLOv11m inference
        # -----------------------------------------------------

        results = self._model(
            frames,
            conf=settings.detection_confidence,
            iou=settings.detection_iou,

            # COCO class 0 = person
            classes=[0],

            verbose=False,
        )

        latency_ms = (
            time.monotonic() - start_time
        ) * 1000

        # -----------------------------------------------------
        # Convert YOLO results
        # -----------------------------------------------------

        detections_per_frame: list[
            list[RawDetection]
        ] = []

        for result in results:

            frame_detections: list[
                RawDetection
            ] = []

            if (
                result.boxes is not None
                and len(result.boxes) > 0
            ):

                boxes = (
                    result.boxes.data
                    .cpu()
                    .numpy()
                )

                for box in boxes:

                    (
                        x1,
                        y1,
                        x2,
                        y2,
                        confidence,
                        class_id,
                    ) = box

                    frame_detections.append(
                        RawDetection(
                            x1=float(x1),
                            y1=float(y1),
                            x2=float(x2),
                            y2=float(y2),
                            confidence=float(
                                confidence
                            ),
                            class_id=int(
                                class_id
                            ),
                        )
                    )

            detections_per_frame.append(
                frame_detections
            )

        total_detections = sum(
            len(detections)
            for detections in detections_per_frame
        )

        logger.debug(
            "yolov11m_batch "
            "frames=%d "
            "detections=%d "
            "latency_ms=%.1f",
            len(frames),
            total_detections,
            latency_ms,
        )

        return (
            detections_per_frame,
            latency_ms,
        )