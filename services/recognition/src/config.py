import os


class RecognitionConfig:
    # Redis
    REDIS_HOST: str = os.environ.get("REDIS_HOST", "redis")
    REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))

    # PostgreSQL
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    # MinIO
    MINIO_ENDPOINT: str = os.environ.get("MINIO_ENDPOINT", "minio:9000")
    MINIO_ACCESS_KEY: str = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
    MINIO_SECRET_KEY: str = os.environ.get("MINIO_SECRET_KEY", "minioadmin")

    # Model — single pack, no per-camera-profile switching. This service
    # runs one use case against one pack; CameraProfile-based model
    # selection was removed deliberately (see model_loader.py).
    MODEL_ROOT: str = os.environ.get("MODEL_ROOT", "/models/recognition")
    RECOGNITION_MODEL_PACK: str = os.environ.get("RECOGNITION_MODEL_PACK", "buffalo_s")
    USE_GPU: bool = os.environ.get("USE_GPU", "false").lower() == "true"
    DET_SIZE: tuple[int, int] = (640, 640)

    # Streams
    DETECTIONS_STREAM: str = "events:detections"
    RECOGNITIONS_STREAM: str = "events:recognitions"
    RECOGNITIONS_MAXLEN: int = 10000
    CONSUMER_GROUP: str = "recognition_group"
    CONSUMER_NAME: str = os.environ.get("RECOGNITION_CONSUMER_NAME", "recognition_worker_1")

    # Cache invalidation channels
    ENROLL_INVALIDATE_CHANNEL: str = "cache:enrolled:invalidate"
    CAMERA_CONFIG_INVALIDATE_CHANNEL: str = "cache:camera_config:invalidate"

    # Recognition thresholds — cosine similarity cutoffs.
    # NOTE: these are schema defaults, not measured constants. Recalibrate
    # against the enrolled-vs-impostor score distribution for the pack
    # actually in use (buffalo_s) before trusting identity_tag in production.
    ENROLLED_THRESHOLD: float = float(os.environ.get("ENROLLED_THRESHOLD", "0.75"))
    VISITOR_THRESHOLD: float = float(os.environ.get("VISITOR_THRESHOLD", "0.60"))

    # Quality gate defaults (per-camera overrides come from camera_config table)
    DEFAULT_MIN_FACE_SIZE_PX: int = int(os.environ.get("DEFAULT_MIN_FACE_SIZE_PX", "40"))
    DEFAULT_BLUR_THRESHOLD: float = float(os.environ.get("DEFAULT_BLUR_THRESHOLD", "100.0"))
    DEFAULT_POSE_YAW_MAX: float = float(os.environ.get("DEFAULT_POSE_YAW_MAX", "45.0"))
    DEFAULT_POSE_PITCH_MAX: float = float(os.environ.get("DEFAULT_POSE_PITCH_MAX", "30.0"))
    DEFAULT_DETECTOR_CONFIDENCE_MIN: float = float(os.environ.get("DEFAULT_DETECTOR_CONFIDENCE_MIN", "0.7"))

    # Sampling
    DEFAULT_SAMPLE_RATE: int = int(os.environ.get("DEFAULT_SAMPLE_RATE", "10"))
    QUALITY_IMPROVEMENT_THRESHOLD: float = float(os.environ.get("QUALITY_IMPROVEMENT_THRESHOLD", "0.2"))

    # Per-track sampler state has no explicit "track ended" signal from the
    # detection worker, so it is aged out instead of retained forever.
    STALE_TRACK_TTL_SECONDS: float = float(os.environ.get("STALE_TRACK_TTL_SECONDS", "120.0"))
    STALE_TRACK_SWEEP_INTERVAL_SECONDS: float = float(
        os.environ.get("STALE_TRACK_SWEEP_INTERVAL_SECONDS", "30.0")
    )


config = RecognitionConfig()
