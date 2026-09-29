"""Face enrollment with the SAME pipeline as the recognition service.

Same model pack (MODEL_ROOT / RECOGNITION_MODEL_PACK via RecognitionModelLoader),
same quality gate thresholds (services/recognition/src/config.py), same
embedding (extract_from_face: unit-normalised 512-d ArcFace). Per photo:

  detect_faces(photo) -> exactly one face, else skipped
  FaceCropper crop of that face -> precheck_size_blur (size, blur)
  evaluate_face (detector confidence, pose) -> extract_from_face

InsightFace is imported and loaded on the first photo only, so a dry-run or a
run without new photos never loads it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class EnrollResult:
    embedding: np.ndarray | None
    quality_score: float = 0.0
    reason: str | None = None  # why the photo was skipped


class ServiceFaceEnroller:
    def __init__(self):
        self._loader = None

    def _load(self):
        if self._loader is None:
            from services.recognition.src.model_loader import RecognitionModelLoader

            loader = RecognitionModelLoader()
            loader.preload()  # raises ModelPackMissing with a clear message
            self._loader = loader
        return self._loader

    def embed(self, image: np.ndarray) -> EnrollResult:
        from services.recognition.src.config import config
        from services.recognition.src.embedding_extractor import extract_from_face
        from services.recognition.src.face_crop import FaceCropper
        from services.recognition.src.quality_gate import QualityGate
        from shared.schemas.common import BoundingBox

        faces = self._load().detect_faces(image)
        if not faces:
            return EnrollResult(None, reason="no face found")
        if len(faces) > 1:
            return EnrollResult(None, reason=f"{len(faces)} faces in the photo (need exactly one)")
        face = faces[0]
        h, w = image.shape[:2]
        x1, y1, x2, y2 = (float(v) for v in face.bbox)
        def clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        box = BoundingBox(x1=clamp(x1 / w), y1=clamp(y1 / h), x2=clamp(x2 / w), y2=clamp(y2 / h))
        crop = FaceCropper().crop(image, box)
        if crop is None:
            return EnrollResult(None, reason="face crop too small")
        gate = QualityGate()
        pre = gate.precheck_size_blur(crop.image, min_face_size_px=config.DEFAULT_MIN_FACE_SIZE_PX,
                                      blur_threshold=config.DEFAULT_BLUR_THRESHOLD,
                                      blur_eval_size=config.BLUR_EVAL_SIZE)
        if not pre.passes:
            return EnrollResult(None, reason=pre.rejection_reason)
        q = gate.evaluate_face(face, blur_score=pre.blur_score, face_size_px=pre.face_size_px,
                               pose_yaw_max=config.DEFAULT_POSE_YAW_MAX,
                               pose_pitch_max=config.DEFAULT_POSE_PITCH_MAX,
                               detector_confidence_min=config.DEFAULT_DETECTOR_CONFIDENCE_MIN)
        if not q.passes:
            return EnrollResult(None, reason=q.rejection_reason)
        emb = extract_from_face(face)
        if emb is None:
            return EnrollResult(None, reason="no embedding")
        return EnrollResult(emb, quality_score=max(0.0, min(1.0, q.quality_score)))
