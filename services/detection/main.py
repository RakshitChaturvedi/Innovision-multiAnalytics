"""
Detection service entry point.

SIGTERM/SIGINT handling, graceful stop() and exiting non-zero on a consumer
crash come from shared/runner.run_consumers (under Docker/Kubernetes the
process receives SIGTERM; stop() must run so in-flight batches are acked).
GET /health is served on HEALTH_PORT (default 8081).
"""

import asyncio
import logging
import sys

from services.detection.src.config import settings
from services.detection.src.consumer import DetectionConsumer
from shared.health import health_port
from shared.runner import run_consumers

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ),
    handlers=[logging.StreamHandler(sys.stdout)],
)


async def main() -> None:
    await run_consumers(
        "detection",
        [DetectionConsumer()],
        health_port=health_port(8081),
        redis_url=settings.redis_url(),
        database_url=settings.database_url,
    )


if __name__ == "__main__":
    asyncio.run(main())
