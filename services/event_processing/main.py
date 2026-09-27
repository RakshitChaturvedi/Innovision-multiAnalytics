import asyncio
import logging
import sys

from services.event_processing.src.workers.intruder.consumer import (
    IntruderConsumer,
)

from services.event_processing.src.workers.zone_monitor.consumer import (
    ZoneMonitorConsumer,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
)


async def main():

    zone_monitor = ZoneMonitorConsumer()
    intruder = IntruderConsumer()

    try:

        await asyncio.gather(
            zone_monitor.start(),
            intruder.start(),
        )

    except KeyboardInterrupt:

        await zone_monitor.stop()
        await intruder.stop()


if __name__ == "__main__":
    asyncio.run(main())