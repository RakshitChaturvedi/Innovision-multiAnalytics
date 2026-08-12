"""
Quality gate — rejects poor face crops before they reach persistence.

Split into two stages so model inference is only paid for once it's
worth it:

  1. precheck_size_blur() — pixel size + Laplacian blur variance.
     No model call. Cheap enough to run on every sampled crop.

  2. evaluate_face()      — pose angle + detector confidence, operating on
     a Face object the CALLER already produced via
     RecognitionModelLoader.detect_best_face(). This function does not
     call the model itself — that call happens exactly once per crop,
     in the consumer, and its result is reused here and for embedding
     extraction.
"""
import logging
from dataclasses import dataclass

import cv2
import numpy as np
from insightface.app.common import Face

logger = logging.getLogger(__name__)


@dataclass
class PrecheckResult:
    passes: bool
    face_size_px: int
    blur_score: float
    rejection_reason: str | None


@dataclass
class QualityResult:
    passes: bool
    quality_score: float  # composite 0.0-1.0
    rejection_reason: str | None


class QualityGate:
    def precheck_size_blur(
        self,
        face_crop: np.ndarray,
        min_face_size_px: int = 40,
        blur_threshold: float = 100.0,
    ) -> PrecheckResult:
        h, w = face_crop.shape[:2]
        face_size_px = min(h, w)

        if face_size_px < min_face_size_px:
            return PrecheckResult(
                passes=False,
                face_size_px=face_size_px,
                blur_score=0.0,
                rejection_reason=f"too_small:{face_size_px}px",
            )

        gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
        blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if blur_score < blur_threshold:
            return PrecheckResult(
                passes=False,
                face_size_px=face_size_px,
                blur_score=blur_score,
                rejection_reason=f"too_blurry:{blur_score:.1f}",
            )

        return PrecheckResult(
            passes=True,
            face_size_px=face_size_px,
            blur_score=blur_score,
            rejection_reason=None,
        )

    def evaluate_face(
        self,
        face: Face,
        blur_score: float,
        face_size_px: int,
        pose_yaw_max: float = 45.0,
        pose_pitch_max: float = 30.0,
        detector_confidence_min: float = 0.7,
    ) -> QualityResult:
        if face.det_score < detector_confidence_min:
            return QualityResult(
                passes=False,
                quality_score=0.0,
                rejection_reason=f"low_confidence:{face.det_score:.2f}",
            )

        pose = getattr(face, "pose", None)
        if pose is not None:
            yaw = abs(float(pose[0]))
            pitch = abs(float(pose[1]))
            if yaw > pose_yaw_max:
                return QualityResult(
                    passes=False,
                    quality_score=0.0,
                    rejection_reason=f"extreme_yaw:{yaw:.1f}",
                )
            if pitch > pose_pitch_max:
                return QualityResult(
                    passes=False,
                    quality_score=0.0,
                    rejection_reason=f"extreme_pitch:{pitch:.1f}",
                )

        blur_norm = min(blur_score / 1000.0, 1.0)
        size_norm = min(face_size_px / 200.0, 1.0)
        conf_score = float(face.det_score)
        quality_score = (blur_norm * 0.3) + (size_norm * 0.3) + (conf_score * 0.4)

        return QualityResult(passes=True, quality_score=quality_score, rejection_reason=None)
