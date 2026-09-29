import os


class HeadcountConfig:
    REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
    REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/innovision_analytics",
    )

    DETECTIONS_STREAM = "events:detections"
    ALERTS_STREAM = "alerts:live"
    CONSUMER_GROUP = "headcount_group"
    CONSUMER_NAME = os.environ.get(
        "HEADCOUNT_CONSUMER_NAME",
        "headcount_worker_1"
    )

    # Aggregation tuning (all durations are EVENT-time seconds unless noted)
    ROLLING_WINDOW_SECONDS = float(os.environ.get("HEADCOUNT_ROLLING_WINDOW_SECONDS", "30"))
    SNAPSHOT_INTERVAL_SECONDS = float(os.environ.get("HEADCOUNT_SNAPSHOT_INTERVAL_SECONDS", "5"))
    BREACH_ENTER_SECONDS = float(os.environ.get("HEADCOUNT_BREACH_ENTER_SECONDS", "5"))
    BREACH_EXIT_SECONDS = float(os.environ.get("HEADCOUNT_BREACH_EXIT_SECONDS", "10"))

    # WALL-clock seconds without detection events before a camera is stale
    STALE_CAMERA_S = float(os.environ.get("HEADCOUNT_STALE_CAMERA_S", "30"))
    SWEEP_INTERVAL_S = float(os.environ.get("HEADCOUNT_SWEEP_INTERVAL_S", "5"))


    ALERTS_MAXLEN = 10000

    # Redis: headcount:current:{zone_id} -> {count, rolling_avg, ts}
    CURRENT_KEY_PREFIX = "headcount:current"
    CURRENT_TTL_SECONDS = 60


config = HeadcountConfig()
