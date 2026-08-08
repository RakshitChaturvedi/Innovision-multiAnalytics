from pydantic_settings import BaseSettings
from pydantic import Field

class DetectionSettings(BaseSettings):
    redis_host: str=Field(default="redis", env="REDIS_HOST")
    redis_port: int=Field(default=6379, env="REDIS_PORT")
    database_url : str = Field(default="",alias="Database URL")
    minio_endpoint: str= Field(default="minio:9000", env="MINIO_ENDPOINT")
    minio_access_key: str= Field(default="minioadmin", env="MINIO_ACCESS_KEY")
    minio_secret_key: str= Field(default="minioadmin", env="MINIO_SECRET_KEY")

    yolov11m_path: str=Field(
        default="/models/detection/yolov11m.onnx", alias="YOLOV11M_PATH"
    )

    detection_confidence: float = Field(default=0.3, env="DETECTION_CONFIDENCE")
    detection_iou: float = Field(default=0.45, env="DETECTION_IOU")
    use_gpu: bool = Field(default=False, env="USE_GPU")

    batch_size: int =Field(default=8, env="BATCH_SIZE")
    batch_timeout_ms: int =Field(default=1000, env="BATCH_TIMEOUT_MS")

    detections_stream: str = "events:detections"
    detections_maxlen: int = 10000

    consumer_group: str = "detection_group"

    consumer_name: str = Field(
        default="detection_worker_1",
        alias="DETECTION_CONSUMER_NAME"
    )

    face_region_ratio: float = Field(
        default=0.35,
        alias="FACE_REGION_RATIO"
    )

    face_area_min: float = Field(
        default=0.001,
        alias="FACE_AREA_MIN"
    )

    test_camera_id: str ="0000000-0000-0000-0000-000000000001"
    class Config:
        env_file = ".env"
        populate_by_name = True
settings = DetectionSettings()