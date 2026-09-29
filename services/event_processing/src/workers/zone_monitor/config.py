from typing import ClassVar

from pydantic import Field

from shared.service_settings import ServiceSettings


class ZoneMonitorConfig(ServiceSettings):
    """Environment first, then .env, then these defaults."""

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    DATABASE_URL: str = (
        "postgresql+asyncpg://analytics:analytics@postgres:5432/innovision_analytics"
    )

    DETECTIONS_STREAM: ClassVar[str] = "events:detections"
    ZONE_EVENTS_STREAM: ClassVar[str] = "events:zone"

    ZONE_EVENTS_MAXLEN: ClassVar[int] = 10000

    CONSUMER_GROUP: ClassVar[str] = "zone_monitor_group"

    CONSUMER_NAME: str = Field(
        default="zone_monitor_worker_1", validation_alias="ZONE_MONITOR_CONSUMER_NAME"
    )

    ZONE_CONFIG_CACHE_PREFIX: ClassVar[str] = "cache:zone_config"
    ZONE_CONFIG_CACHE_TTL: ClassVar[int] = 300
    # In-process cache TTL (seconds)
    ZONE_CACHE_TTL_S: float = 30.0

    # Per-camera hash: field track_id -> JSON track state
    ACTIVE_TRACKS_PREFIX: ClassVar[str] = "zone:active"
    # Per-camera bookkeeping for the stale-camera sweeper
    CAMERA_META_PREFIX: ClassVar[str] = "zone:meta"
    ACTIVE_STATE_TTL: ClassVar[int] = 3600

    # A track missing from an event for longer than this is exited (event time)
    LOST_TRACK_TIMEOUT_S: float = 2.0
    # A camera with no events for this long (wall clock) is swept
    STALE_CAMERA_S: float = 10.0
    SWEEP_INTERVAL_S: float = Field(default=5.0, validation_alias="ZONE_SWEEP_INTERVAL_S")

    DEFAULT_DWELL_THRESHOLD_SECONDS: int = 60


config = ZoneMonitorConfig()
