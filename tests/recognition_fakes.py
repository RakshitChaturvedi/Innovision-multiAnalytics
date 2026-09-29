"""Recognition fakes shared by the recognition tests and the cross-service
identity-swap scenario (tests/services/event_processing/workers/intruder).

Importing this module also installs an `insightface` import stub when the
real package is missing, so services.recognition can be imported."""
import sys
import types
from dataclasses import dataclass, field

import cv2
import numpy as np

# services/recognition/src/model_loader.py imports insightface at module level
# (model loading is not to be touched). Provide an import stub if the package
# is not installed; the model itself is always a fake in these tests.
try:  # pragma: no cover
    import insightface  # noqa: F401
except ImportError:
    pkg = types.ModuleType("insightface")
    app = types.ModuleType("insightface.app")
    common = types.ModuleType("insightface.app.common")
    app.FaceAnalysis = type("FaceAnalysis", (), {})
    common.Face = type("Face", (), {})
    app.common = common
    pkg.app = app
    sys.modules.update({"insightface": pkg, "insightface.app": app, "insightface.app.common": common})


# ----------------------------------------------------------------- fake model


@dataclass
class FakeFace:
    bbox: tuple = (5.0, 5.0, 60.0, 60.0)
    det_score: float = 0.99
    pose: tuple = (0.0, 0.0, 0.0)  # InsightFace order: [pitch, yaw, roll]
    embedding: np.ndarray = field(default_factory=lambda: _unit(0))


class FrameFaceLoader:
    """Fake model whose faces are placed in FRAME pixels.

    `attach(consumer)` wraps the consumer's FaceCropper so the loader knows
    where the current crop sits; it then returns, in crop-local pixels, every
    face whose center lies inside that crop, like the real model would.
    Implements both the old single-face API and `detect_faces`.
    """

    def __init__(self, faces: list[FakeFace]):
        self.faces = faces  # bbox in frame pixels
        self.origin = (0, 0)
        self.crop_calls = 0

    def attach(self, consumer) -> "FrameFaceLoader":
        crop = consumer._cropper.crop

        def recording_crop(frame, bbox):
            result = crop(frame, bbox)
            if result is not None:
                self.origin = (result.x1, result.y1)
                self.shape = result.image.shape[:2]
            return result

        consumer._cropper.crop = recording_crop
        consumer._model_loader = self
        return self

    def detect_faces(self, crop):
        self.crop_calls += 1
        ox, oy = self.origin
        h, w = crop.shape[:2]
        out = []
        for f in self.faces:
            cx, cy = (f.bbox[0] + f.bbox[2]) / 2 - ox, (f.bbox[1] + f.bbox[3]) / 2 - oy
            if 0 <= cx < w and 0 <= cy < h:
                x1, y1, x2, y2 = f.bbox
                out.append(FakeFace(bbox=(x1 - ox, y1 - oy, x2 - ox, y2 - oy),
                                    det_score=f.det_score, pose=f.pose, embedding=f.embedding))
        return out

    def detect_best_face(self, crop):  # pre-fix API
        faces = self.detect_faces(crop)
        return max(faces, key=lambda f: f.det_score) if faces else None


def face_at(cx: float, cy: float, frame_w: int, frame_h: int, embedding, det_score=0.9, size=24.0):
    """FakeFace centered at normalized (cx, cy), bbox in frame pixels."""
    x, y = cx * frame_w, cy * frame_h
    return FakeFace(bbox=(x - size / 2, y - size / 2, x + size / 2, y + size / 2),
                    det_score=det_score, embedding=embedding)


def person_track(track_id: int, x1: float, y1: float, x2: float, y2: float, face_ratio=0.35) -> dict:
    """Track dict as detection emits it: face_bbox = full-width top part of the box."""
    box = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    face = {"x1": x1, "y1": y1, "x2": x2, "y2": y1 + (y2 - y1) * face_ratio}
    return {"track_id": track_id, "bbox": box, "confidence": 0.9, "class_label": "person",
            "has_face": True, "face_bbox": face}


def _unit(i: int, dim: int = 512) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


unit = _unit



def jpeg(w=320, h=240) -> bytes:
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()
