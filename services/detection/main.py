"""
Detection service entry point.
"""

import asyncio
import logging
import sys

from src.consumer import DetectionConsumer


# =============================================================
# LOGGING
# =============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(
            sys.stdout
        )
    ],
)

logger = logging.getLogger(__name__)


# =============================================================
# MAIN
# =============================================================

async def main() -> None:

    logger.info(
        "detection_service_starting"
    )

    consumer = DetectionConsumer()

    try:

        await consumer.start()

    except KeyboardInterrupt:

        logger.info(
            "detection_service_stopping"
        )

    finally:

        await consumer.stop()


# =============================================================
# ENTRY POINT
# =============================================================

if __name__ == "__main__":

    asyncio.run(main())