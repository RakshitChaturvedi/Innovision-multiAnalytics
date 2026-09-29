import os


class ZoneMonitorConfig:
    REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
    REDIS_PORT = int(
        os.environ.get("REDIS_PORT", "6379")
    )

    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/innovision_analytics",
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
    # In-process cache TTL (seconds)
    ZONE_CACHE_TTL_S = float(
        os.environ.get("ZONE_CACHE_TTL_S", "30")
    )

    # Per-camera hash: field track_id -> JSON track state
    ACTIVE_TRACKS_PREFIX = "zone:active"
    # Per-camera bookkeeping for the stale-camera sweeper
    CAMERA_META_PREFIX = "zone:meta"
    ACTIVE_STATE_TTL = 3600

    # A track missing from an event for longer than this is exited (event time)
    LOST_TRACK_TIMEOUT_S = float(
        os.environ.get("LOST_TRACK_TIMEOUT_S", "2.0")
    )
    # A camera with no events for this long (wall clock) is swept
    STALE_CAMERA_S = float(
        os.environ.get("STALE_CAMERA_S", "10")
    )
    SWEEP_INTERVAL_S = float(
        os.environ.get("ZONE_SWEEP_INTERVAL_S", "5")
    )

    DEFAULT_DWELL_THRESHOLD_SECONDS = int(
        os.environ.get(
            "DEFAULT_DWELL_THRESHOLD_SECONDS",
            "60",
        )
    )


config = ZoneMonitorConfig()
