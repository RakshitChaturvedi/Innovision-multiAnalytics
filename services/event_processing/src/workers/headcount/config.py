from typing import ClassVar

from pydantic import Field

from shared.service_settings import ServiceSettings


class HeadcountConfig(ServiceSettings):
    """Environment first, then .env, then these defaults."""

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    DATABASE_URL: str = (
        "postgresql+asyncpg://analytics:analytics@postgres:5432/innovision_analytics"
    )

    DETECTIONS_STREAM: ClassVar[str] = "events:detections"
    ALERTS_STREAM: ClassVar[str] = "alerts:live"
    CONSUMER_GROUP: ClassVar[str] = "headcount_group"
    CONSUMER_NAME: str = Field(
        default="headcount_worker_1", validation_alias="HEADCOUNT_CONSUMER_NAME"
    )

    # Aggregation tuning (all durations are EVENT-time seconds unless noted)
    ROLLING_WINDOW_SECONDS: float = Field(
        default=30.0, validation_alias="HEADCOUNT_ROLLING_WINDOW_SECONDS"
    )
    SNAPSHOT_INTERVAL_SECONDS: float = Field(
        default=5.0, validation_alias="HEADCOUNT_SNAPSHOT_INTERVAL_SECONDS"
    )
    BREACH_ENTER_SECONDS: float = Field(
        default=5.0, validation_alias="HEADCOUNT_BREACH_ENTER_SECONDS"
    )
    BREACH_EXIT_SECONDS: float = Field(
        default=10.0, validation_alias="HEADCOUNT_BREACH_EXIT_SECONDS"
    )

    # WALL-clock seconds without detection events before a camera is stale
    STALE_CAMERA_S: float = Field(default=30.0, validation_alias="HEADCOUNT_STALE_CAMERA_S")
    SWEEP_INTERVAL_S: float = Field(default=5.0, validation_alias="HEADCOUNT_SWEEP_INTERVAL_S")

    ALERTS_MAXLEN: ClassVar[int] = 10000

    # Redis: headcount:current:{zone_id} -> {count, rolling_avg, ts}
    CURRENT_KEY_PREFIX: ClassVar[str] = "headcount:current"
    CURRENT_TTL_SECONDS: ClassVar[int] = 60


config = HeadcountConfig()
