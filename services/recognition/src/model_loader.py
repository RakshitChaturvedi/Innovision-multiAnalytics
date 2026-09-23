"""
Loads the single InsightFace model pack used by the recognition worker.

Per-camera-profile model switching (buffalo_s / buffalo_m / buffalo_l keyed
off CameraProfile) has been removed by design decision — this service runs
one use case against one pack, and CameraProfile has been dropped from the
detection worker's output entirely (DetectionEvent carries no profile
field). Pack identity is configurable via RECOGNITION_MODEL_PACK, not
branched in code.
"""
import logging
import os

from insightface.app import FaceAnalysis
from insightface.app.common import Face

from .config import config

logger = logging.getLogger(__name__)


class RecognitionModelLoader:
    """
    Loads and holds the single FaceAnalysis pack (SCRFD detector + 5-point
    alignment + ArcFace embedding extractor) used for this service's
    lifetime.
    """

    def __init__(self) -> None:
        self._app: FaceAnalysis | None = None

    def preload(self) -> None:
        """Loads the configured pack. Blocks until complete. Idempotent."""
        if self._app is not None:
            return

        pack_path = os.path.join(config.MODEL_ROOT, config.RECOGNITION_MODEL_PACK)
        logger.info(
            "loading_insightface_pack pack=%s path=%s gpu=%s",
            config.RECOGNITION_MODEL_PACK, pack_path, config.USE_GPU,
        )

        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if config.USE_GPU
            else ["CPUExecutionProvider"]
        )

        app = FaceAnalysis(
            name=config.RECOGNITION_MODEL_PACK,
            root=config.MODEL_ROOT,
            providers=providers,
        )
        app.prepare(ctx_id=0 if config.USE_GPU else -1, det_size=config.DET_SIZE)

        self._app = app
        logger.info("insightface_pack_loaded pack=%s", config.RECOGNITION_MODEL_PACK)

    def get_model(self) -> FaceAnalysis:
        if self._app is None:
            raise RuntimeError(
                "Model pack not loaded. Call preload() before accepting frames."
            )
        return self._app

    def detect_best_face(self, face_crop) -> Face | None:
        """
        Runs the full SCRFD + ArcFace pipeline ONCE on a face crop and
        returns the highest-confidence Face — bbox, 5-point landmarks,
        pose, det_score, AND the 512-d embedding are all attached to the
        object InsightFace hands back from a single app.get() call.

        Quality evaluation and embedding extraction both read off this one
        Face object instead of re-invoking the model — avoids running
        detection + recognition twice per crop.

        This call is blocking (CPU or GPU bound). Callers should invoke it
        via asyncio.to_thread (or an executor) so it doesn't stall the
        event loop — see consumer.py.
        """
        app = self.get_model()
        faces = app.get(face_crop)
        if not faces:
            return None
        return max(faces, key=lambda f: f.det_score)
