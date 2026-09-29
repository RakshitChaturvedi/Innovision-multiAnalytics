from typing import ClassVar

from pydantic import Field

from shared.service_settings import ServiceSettings


class IntruderConfig(ServiceSettings):
    """Environment first, then .env, then these defaults."""

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    DATABASE_URL: str = (
        "postgresql+asyncpg://analytics:analytics@postgres:5432/innovision_analytics"
    )

    ZONE_EVENTS_STREAM: ClassVar[str] = "events:zone"

    ALERTS_STREAM: ClassVar[str] = "alerts:live"

    CONSUMER_GROUP: ClassVar[str] = "intruder_group"

    CONSUMER_NAME: str = Field(
        default="intruder_worker_1", validation_alias="INTRUDER_CONSUMER_NAME"
    )

    ZONE_EVENTS_MAXLEN: ClassVar[int] = 10000

    ALERTS_MAXLEN: ClassVar[int] = 10000

    # Recognition can arrive slightly before or after
    # the zone event. The worker retries briefly.
    RECOGNITION_LOOKUP_RETRIES: int = Field(
        default=5, validation_alias="INTRUDER_RECOGNITION_LOOKUP_RETRIES"
    )

    RECOGNITION_LOOKUP_DELAY_SECONDS: float = Field(
        default=0.3, validation_alias="INTRUDER_RECOGNITION_LOOKUP_DELAY_SECONDS"
    )

    # Only recognitions in [zone_ts - MAX_AGE, zone_ts + FUTURE] count.
    RECOGNITION_MAX_AGE_S: float = 30.0

    RECOGNITION_FUTURE_S: float = 5.0

    # > 0: a new intruder event within this many seconds (event time) of an
    # already published alert for the same camera+zone is recorded with
    # alert_suppressed = true and NOT published. 0 disables the cooldown.
    ALERT_COOLDOWN_S: float = Field(
        default=0.0, ge=0, validation_alias="INTRUDER_ALERT_COOLDOWN_S"
    )

    # Identity ownership across tracks only counts rows from the last
    # RECOGNITION_RECENT_S (event time) before the decision.
    RECOGNITION_RECENT_S: float = Field(
        default=5.0, validation_alias="RECOGNITION_RECENT_S"
    )

    # Restricted zone without an authorized winner: wait this long (event
    # time) for more recognition rows before alerting. Blocklist: no wait.
    INTRUDER_GRACE_S: float = Field(default=4.0, validation_alias="INTRUDER_GRACE_S")

    # Wall-clock sweeper over pending candidates (cameras that go quiet,
    # restarts) and alerts still owed after a failed publish.
    INTRUDER_SWEEP_INTERVAL_S: float = Field(
        default=0.5, validation_alias="INTRUDER_SWEEP_INTERVAL_S"
    )


config = IntruderConfig()
