from typing import ClassVar

from pydantic import Field

from shared.config import settings
from shared.service_settings import ServiceSettings


class RecognitionConfig(ServiceSettings):
    """Environment first, then .env, then these defaults."""

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    # PostgreSQL
    DATABASE_URL: str = Field(default_factory=lambda: settings.DATABASE_URL)

    # MinIO
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"

    # Model — single pack, no per-camera-profile switching. This service
    # runs one use case against one pack; CameraProfile-based model
    # selection was removed deliberately (see model_loader.py).
    MODEL_ROOT: str = "/models/recognition"
    RECOGNITION_MODEL_PACK: str = "buffalo_s"
    USE_GPU: bool = False
    DET_SIZE: ClassVar[tuple[int, int]] = (640, 640)

    # Streams
    DETECTIONS_STREAM: ClassVar[str] = "events:detections"
    RECOGNITIONS_STREAM: ClassVar[str] = "events:recognitions"
    RECOGNITIONS_MAXLEN: ClassVar[int] = 10000
    CONSUMER_GROUP: ClassVar[str] = "recognition_group"
    CONSUMER_NAME: str = Field(
        default="recognition_worker_1", validation_alias="RECOGNITION_CONSUMER_NAME"
    )

    # Cache invalidation channels
    ENROLL_INVALIDATE_CHANNEL: ClassVar[str] = "cache:enrolled:invalidate"
    CAMERA_CONFIG_INVALIDATE_CHANNEL: ClassVar[str] = "cache:camera_config:invalidate"

    # Recognition thresholds — cosine similarity cutoffs.
    # NOTE: these are schema defaults, not measured constants. Recalibrate
    # against the enrolled-vs-impostor score distribution for the pack
    # actually in use (buffalo_s) before trusting identity_tag in production.
    ENROLLED_THRESHOLD: float = 0.75
    VISITOR_THRESHOLD: float = 0.60

    # Quality gate defaults (per-camera overrides come from camera_config table)
    DEFAULT_MIN_FACE_SIZE_PX: int = 40
    DEFAULT_BLUR_THRESHOLD: float = 100.0
    DEFAULT_POSE_YAW_MAX: float = 45.0
    DEFAULT_POSE_PITCH_MAX: float = 30.0
    DEFAULT_DETECTOR_CONFIDENCE_MIN: float = 0.7

    # Face -> track assignment (face_selection.py). Head region of a person
    # box: central HEAD_WIDTH_FRAC of its width, from HEAD_TOP_MARGIN_FRAC of
    # its height above the top down to HEAD_HEIGHT_FRAC below it.
    HEAD_WIDTH_FRAC: float = Field(default=0.7, validation_alias="RECOGNITION_HEAD_WIDTH_FRAC")
    HEAD_TOP_MARGIN_FRAC: float = Field(
        default=0.05, validation_alias="RECOGNITION_HEAD_TOP_MARGIN_FRAC"
    )
    HEAD_HEIGHT_FRAC: float = Field(default=0.30, validation_alias="RECOGNITION_HEAD_HEIGHT_FRAC")
    # Two faces competing for one track are ambiguous (nothing recorded)
    # when the farther one is within this ratio of the nearer one.
    FACE_AMBIGUITY_RATIO: float = Field(
        default=1.25, validation_alias="RECOGNITION_FACE_AMBIGUITY_RATIO"
    )

    # Sampling
    DEFAULT_SAMPLE_RATE: int = 10
    QUALITY_IMPROVEMENT_THRESHOLD: float = 0.2

    # Per-track sampler state has no explicit "track ended" signal from the
    # detection worker, so it is aged out instead of retained forever.
    STALE_TRACK_TTL_SECONDS: float = 120.0
    STALE_TRACK_SWEEP_INTERVAL_SECONDS: float = 30.0


config = RecognitionConfig()
