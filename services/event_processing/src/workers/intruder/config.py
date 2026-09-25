import os


class IntruderConfig:
    REDIS_HOST = os.environ.get(
        "REDIS_HOST",
        "redis",
    )

    REDIS_PORT = int(
        os.environ.get(
            "REDIS_PORT",
            "6379",
        )
    )

    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/innovision",
    )

    ZONE_EVENTS_STREAM = "events:zone"

    ALERTS_STREAM = "alerts:live"

    CONSUMER_GROUP = "intruder_group"

    CONSUMER_NAME = os.environ.get(
        "INTRUDER_CONSUMER_NAME",
        "intruder_worker_1",
    )

    ZONE_EVENTS_MAXLEN = 10000

    ALERTS_MAXLEN = 10000

    # Recognition can arrive slightly before or after
    # the zone event. The worker retries briefly.
    RECOGNITION_LOOKUP_RETRIES = int(
        os.environ.get(
            "INTRUDER_RECOGNITION_LOOKUP_RETRIES",
            "5",
        )
    )

    RECOGNITION_LOOKUP_DELAY_SECONDS = float(
        os.environ.get(
            "INTRUDER_RECOGNITION_LOOKUP_DELAY_SECONDS",
            "0.2",
        )
    )


config = IntruderConfig()