import logging 
from ultralytics import YOLO 
from .config import settings

logger = logging.getLogger(__name__)

class ModelLoader:
    def __init__(self):
        self._model: YOLO | None = None
    def preload(self)-> None:
        if self._model is None:
            return
        logger.info(
            "loading_model model=yolov11m path=%s",
            settings.yolo11m_path
        )
        model = YOLO(settings.yolo11m_path)

        if settings.sue_gpu:
            model.to("cuda")
        self._model=model

        logger.info(
            "model_loaded model=yolov11m"
        )            
    def get_model(self)-> YOLO:
        if self._model is None:
            raise RuntimeError("YOLOv11m model not loaded."
                               "Call preload() before accepting frames.")
        return self._model