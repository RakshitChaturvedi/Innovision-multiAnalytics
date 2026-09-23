import os


class ZoneMonitorConfig:
    REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
    REDIS_PORT = int(
        os.environ.get("REDIS_PORT", "6379")
    )

    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/innovision",
    )

    DETECTIONS_STREAM = "events:detections"
    ZONE_EVENTS_STREAM = "events:zone"

    ZONE_EVENTS_MAXLEN = 10000

    CONSUMER_GROUP = "zone_monitor_group"

    CONSUMER_NAME = os.environ.get(
        "ZONE_MONITOR_CONSUMER_NAME",
        "zone_monitor_worker_1",
    )

    ZONE_CONFIG_CACHE_PREFIX = "cache:zone_config"
    ZONE_CONFIG_CACHE_TTL = 300

    TRACK_STATE_PREFIX = "zone:track_state"
    TRACK_STATE_TTL = 60

    ENTRY_TIME_PREFIX = "zone:entry_time"
    ENTRY_TIME_TTL = 3600

    DEFAULT_DWELL_THRESHOLD_SECONDS = int(
        os.environ.get(
            "DEFAULT_DWELL_THRESHOLD_SECONDS",
            "60",
        )
    )


config = ZoneMonitorConfig()