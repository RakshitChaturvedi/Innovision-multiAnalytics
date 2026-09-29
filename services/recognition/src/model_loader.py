"""
Loads the single InsightFace model pack used by the recognition worker.

Per-camera-profile model switching (buffalo_s / buffalo_m / buffalo_l keyed
off CameraProfile) has been removed by design decision — this service runs
one use case against one pack, and CameraProfile has been dropped from the
detection worker's output entirely (DetectionEvent carries no profile
field). Pack identity is configurable via RECOGNITION_MODEL_PACK, not
branched in code.
"""
import glob
import logging
import os

from insightface.app import FaceAnalysis
from insightface.app.common import Face

from .config import config

logger = logging.getLogger(__name__)


class ModelPackMissing(RuntimeError):
    """The InsightFace model pack folder is missing or holds no .onnx models."""


def model_pack_path(model_root: str, pack: str) -> str:
    """Where InsightFace's FaceAnalysis(name=pack, root=model_root) loads from."""
    return os.path.join(model_root, "models", pack)


def check_model_pack(model_root: str, pack: str) -> str:
    """Return the pack folder, or raise ModelPackMissing with a clear message."""
    path = model_pack_path(model_root, pack)
    if not os.path.isdir(path):
        message = (
            f"InsightFace model pack '{pack}' not found: folder {path} does not exist. "
            f"InsightFace loads MODEL_ROOT/models/<RECOGNITION_MODEL_PACK>; "
            f"MODEL_ROOT={model_root!r} must be the folder that CONTAINS 'models/{pack}'."
        )
    elif not glob.glob(os.path.join(path, "*.onnx")):
        message = f"InsightFace model pack '{pack}' at {path} contains no .onnx files."
    else:
        return path
    logger.error("model_pack_missing %s", message)
    raise ModelPackMissing(message)


class RecognitionModelLoader:
    """
    Loads and holds the single FaceAnalysis pack (SCRFD detector + 5-point
    alignment + ArcFace embedding extractor) used for this service's
    lifetime.
    """

    def __init__(self) -> None:
        self._app: FaceAnalysis | None = None

    def preload(self) -> None:
        """Loads the configured pack. Blocks until complete. Idempotent.
        Raises ModelPackMissing (startup fails) if the pack folder is absent."""
        if self._app is not None:
            return

        # InsightFace loads FaceAnalysis(name, root) from root/models/<name>
        # (and silently tries to DOWNLOAD it when that folder is missing).
        pack_path = check_model_pack(config.MODEL_ROOT, config.RECOGNITION_MODEL_PACK)
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
        logger.info(
            "insightface_pack_loaded pack=%s path=%s", config.RECOGNITION_MODEL_PACK, pack_path,
        )

    def get_model(self) -> FaceAnalysis:
        if self._app is None:
            raise RuntimeError(
                "Model pack not loaded. Call preload() before accepting frames."
            )
        return self._app

    def detect_faces(self, face_crop) -> list[Face]:
        """
        Runs the full SCRFD + ArcFace pipeline ONCE on a crop and returns
        EVERY face found — bbox (crop pixels), 5-point landmarks, pose,
        det_score AND the 512-d embedding come from that single app.get()
        call, so quality evaluation and embedding extraction never re-run
        the model.

        The crop can contain more than one person's face; the caller decides
        which face belongs to the track (face_selection.py). Never pick by
        det_score here: that is how a neighbour's face ended up recorded as
        this track's identity.

        Blocking (CPU or GPU bound): call via asyncio.to_thread.
        """
        app = self.get_model()
        return list(app.get(face_crop) or [])
