import asyncio
import logging
import sys

from services.event_processing.src.workers.zone_monitor.consumer import (
    ZoneMonitorConsumer
)


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
)


async def main():

    consumer = ZoneMonitorConsumer()

    try:
        await consumer.start()

    except KeyboardInterrupt:

        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())