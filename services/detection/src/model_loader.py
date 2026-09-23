"""
YOLOv11m model loader.

Loads the model exactly once per process and hands the same
instance to every batch.
"""

import logging

from ultralytics import YOLO

from .config import settings

logger = logging.getLogger(__name__)


class ModelLoader:

    def __init__(self) -> None:
        self._model: YOLO | None = None

    # ---------------------------------------------------------
    # PRELOAD
    # ---------------------------------------------------------

    def preload(self) -> None:
        """
        Load the model. Idempotent.
        """

        # FIX: guard was inverted, so the model was never loaded.
        if self._model is not None:
            return

        logger.info(
            "loading_model model=yolov11m path=%s",
            settings.yolov11m_path,          # FIX: was settings.yolo11m_path
        )

        model = YOLO(settings.yolov11m_path)

        if settings.use_gpu:                 # FIX: was settings.sue_gpu
            try:
                model.to("cuda")
            except Exception as exc:
                logger.warning(
                    "gpu_unavailable_falling_back_to_cpu error=%s",
                    exc,
                )

        self._model = model

        # Warm up so the first real batch does not pay the
        # lazy-init cost (cuDNN autotune, graph build, etc).
        self._warmup(model)

        logger.info("model_loaded model=yolov11m")

    @staticmethod
    def _warmup(model: YOLO) -> None:
        try:
            import numpy as np

            dummy = np.zeros((640, 640, 3), dtype=np.uint8)
            model(dummy, verbose=False)

            logger.info("model_warmup_complete")

        except Exception as exc:
            logger.warning("model_warmup_failed error=%s", exc)

    # ---------------------------------------------------------
    # GET
    # ---------------------------------------------------------

    def get_model(self) -> YOLO:
        if self._model is None:
            raise RuntimeError(
                "YOLOv11m model not loaded. "
                "Call preload() before accepting frames."
            )
        return self._model
