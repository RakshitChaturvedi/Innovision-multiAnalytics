"""
Detection service settings.

FIXES
-----
1. `database_url` used alias "Database URL" — an alias containing a
   space can never be supplied as an environment variable, so the URL
   was always "" and `create_async_engine("")` failed at startup.
2. Mixing `env=` (pydantic v1) with `alias=` (v2) meant several fields
   silently ignored their environment variables. pydantic-settings v2
   already maps FIELD_NAME -> FIELD_NAME env var, so the redundant
   `env=` arguments are dropped.
3. `extra="ignore"` so a shared .env carrying keys for other services
   does not blow up this one.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DetectionSettings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    # ---------------- infrastructure ----------------

    redis_host: str = Field(default="redis")
    redis_port: int = Field(default=6379)

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@postgres:5432/innovision",
    )

    db_pool_size: int = Field(default=5)
    db_max_overflow: int = Field(default=5)

    minio_endpoint: str = Field(default="minio:9000")
    minio_access_key: str = Field(default="minioadmin")
    minio_secret_key: str = Field(default="minioadmin")

    # ---------------- model ----------------

    yolov11m_path: str = Field(default="/models/detection/yolov11m.pt")

    detection_confidence: float = Field(default=0.3)
    detection_iou: float = Field(default=0.45)
    detection_classes: list[int] = Field(default=[0])
    detection_imgsz: int = Field(default=640)
    use_gpu: bool = Field(default=False)

    # Confidence floor applied AFTER tracking. Kept separate from the
    # YOLO threshold: ByteTrack deliberately uses low-confidence boxes
    # for association, and we do not want those published.
    publish_confidence: float = Field(default=0.5)

    # ---------------- tracking ----------------

    tracker_frame_rate: int = Field(default=10)
    track_high_thresh: float = Field(default=0.5)
    track_low_thresh: float = Field(default=0.1)
    new_track_thresh: float = Field(default=0.6)
    track_buffer: int = Field(default=30)
    match_thresh: float = Field(default=0.8)

    # ---------------- batching ----------------

    batch_size: int = Field(default=8)
    batch_timeout_ms: int = Field(default=100)
    max_pending_batches: int = Field(default=4)

    # ---------------- streams ----------------

    detections_stream: str = Field(default="events:detections")
    detections_maxlen: int = Field(default=10000)

    consumer_group: str = Field(default="detection_group")
    consumer_name: str = Field(default="detection_worker_1")

    frames_bucket: str = Field(default="snapshots")

    # ---------------- face estimation ----------------

    face_region_ratio: float = Field(default=0.35)
    face_area_min: float = Field(default=0.001)

    # ---------------- observability ----------------

    metrics_enabled: bool = Field(default=True)
    metrics_port: int = Field(default=9108)
    log_level: str = Field(default="INFO")

    # ---------------- sprint-2 single camera ----------------

    # NOTE: the original value had 7 digits in the first UUID group
    # instead of 8, so it was not a valid UUID.
    test_camera_id: str = Field(
        default="00000000-0000-0000-0000-000000000001"
    )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"


settings = DetectionSettings()
