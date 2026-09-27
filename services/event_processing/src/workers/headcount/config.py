import os


class HeadcountConfig:
    REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
    REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/innovision",
    )

    DETECTIONS_STREAM = "events:detections"
    ALERTS_STREAM = "alerts:live"
    CONSUMER_GROUP = "headcount_group"
    CONSUMER_NAME = os.environ.get(
        "HEADCOUNT_CONSUMER_NAME", 
        "headcount_worker_1"
    )

    # Aggregation tuning
    ROLLING_WINDOW_SECONDS = int(os.environ.get("HEADCOUNT_ROLLING_WINDOW_SECONDS", "30"))
    SNAPSHOT_INTERVAL_SECONDS = int(os.environ.get("HEADCOUNT_SNAPSHOT_INTERVAL_SECONDS", "5"))
    ALERTS_MAXLEN = 10000

    # Redis key prefixes
    TRACK_STATE_PREFIX = "zone:track_state"
    HEADCOUNT_CACHE_PREFIX = "cache:headcount"
    HEADCOUNT_CACHE_TTL = 60


config = HeadcountConfig()
