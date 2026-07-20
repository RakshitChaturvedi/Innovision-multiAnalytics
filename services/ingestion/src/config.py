from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # app config loaded into env vars
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    service_name: str = "ingestion"
    log_level: str = "INFO"

    camera_id: str = Field(..., description="Unique camera identification id")
    ingestion_source: str = Field(default="video", description="Frame source (video/RTSP)")
    video_path: Path = Field(..., description="Path to local video file when using test mode")

    target_fps: int = Field(default=10, ge=1)
    jpeg_quality: int = Field(default=85, ge=1, le=100)
    frame_width: int = 1920
    frame_height: int = 1080

    redis_url: str = Field(default="redis://redis:6379/0")
    frame_stream_maxlen: int  = Field(default=1000, ge=100)
    frame_stream_name: str = "frames"
    frame_cache_ttl_seconds: int = 60

    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str = "frames"
    minio_secure: bool = False

    heartbeat_interval_seconds: int = Field(default=5, ge=1)
    offline_timeout_seconds: int = Field(default=10, ge=1)

settings = Settings()