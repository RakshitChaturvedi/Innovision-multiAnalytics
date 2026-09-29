"""
Quality gate — rejects poor face crops before they reach persistence.

Split into two stages so model inference is only paid for once it's
worth it:

  1. precheck_size_blur() — pixel size + Laplacian blur variance.
     No model call. Cheap enough to run on every sampled crop.

  2. evaluate_face()      — pose angle + detector confidence, operating on
     a Face object the CALLER already produced via
     RecognitionModelLoader.detect_faces() (the face selected for the track). This function does not
     call the model itself — that call happens exactly once per crop,
     in the consumer, and its result is reused here and for embedding
     extraction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:  # only for annotations; keeps the gate importable without insightface
    from insightface.app.common import Face

logger = logging.getLogger(__name__)

# Blur is scored on a fixed-size, band-limited grayscale copy of the crop, so
# the same face scores about the same whether it is 72 px (640x360 source) or
# 216 px (1920x1080) wide. Raw Laplacian variance falls as a face gets more
# pixels (the platform upscales every source to 1920x1080), which made a sharp
# face fail at 1080p. Calibrated with tools/calibrate_blur.py.
DEFAULT_BLUR_EVAL_SIZE = 112
# Removes the top octave, which a small crop upscaled to BLUR_EVAL_SIZE never
# has; without it a 72 px face scores ~3x lower than the same face at 216 px.
BLUR_PREBLUR_SIGMA = 1.0
# blur_score at which the blur term of the composite quality score saturates.
BLUR_SCORE_FULL = 100.0


def blur_score(face_crop: np.ndarray, eval_size: int = DEFAULT_BLUR_EVAL_SIZE) -> float:
    """Resolution-independent sharpness: Laplacian variance at eval_size px."""
    gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY) if face_crop.ndim == 3 else face_crop
    h, w = gray.shape[:2]
    interp = cv2.INTER_AREA if min(h, w) >= eval_size else cv2.INTER_CUBIC
    gray = cv2.resize(gray, (eval_size, eval_size), interpolation=interp).astype(np.float64)
    gray = cv2.GaussianBlur(gray, (0, 0), BLUR_PREBLUR_SIGMA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


@dataclass
class PrecheckResult:
    passes: bool
    face_size_px: int
    blur_score: float
    rejection_reason: str | None
    reason_code: str | None = None  # too_small | too_blurry


@dataclass
class QualityResult:
    passes: bool
    quality_score: float  # composite 0.0-1.0
    rejection_reason: str | None
    reason_code: str | None = None  # pose | detector_confidence


class QualityGate:
    def precheck_size_blur(
        self,
        face_crop: np.ndarray,
        min_face_size_px: int = 40,
        blur_threshold: float = 15.0,
        blur_eval_size: int = DEFAULT_BLUR_EVAL_SIZE,
    ) -> PrecheckResult:
        h, w = face_crop.shape[:2]
        face_size_px = min(h, w)

        if face_size_px < min_face_size_px:
            return PrecheckResult(
                passes=False,
                face_size_px=face_size_px,
                blur_score=0.0,
                rejection_reason=f"too_small:{face_size_px}px",
                reason_code="too_small",
            )

        score = blur_score(face_crop, blur_eval_size)
        if score < blur_threshold:
            return PrecheckResult(
                passes=False,
                face_size_px=face_size_px,
                blur_score=score,
                rejection_reason=f"too_blurry:{score:.1f}",
                reason_code="too_blurry",
            )

        return PrecheckResult(
            passes=True,
            face_size_px=face_size_px,
            blur_score=score,
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
                reason_code="detector_confidence",
            )

        pose = getattr(face, "pose", None)
        if pose is not None:
            # InsightFace Face.pose is [pitch, yaw, roll] (degrees).
            pitch = abs(float(pose[0]))
            yaw = abs(float(pose[1]))
            if yaw > pose_yaw_max:
                return QualityResult(
                    passes=False,
                    quality_score=0.0,
                    rejection_reason=f"extreme_yaw:{yaw:.1f}",
                    reason_code="pose",
                )
            if pitch > pose_pitch_max:
                return QualityResult(
                    passes=False,
                    quality_score=0.0,
                    rejection_reason=f"extreme_pitch:{pitch:.1f}",
                    reason_code="pose",
                )

        blur_norm = min(blur_score / BLUR_SCORE_FULL, 1.0)
        size_norm = min(face_size_px / 200.0, 1.0)
        conf_score = float(face.det_score)
        quality_score = (blur_norm * 0.3) + (size_norm * 0.3) + (conf_score * 0.4)

        return QualityResult(passes=True, quality_score=quality_score, rejection_reason=None)
