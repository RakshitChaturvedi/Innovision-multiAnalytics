import asyncio
import logging
import sys

from services.event_processing.src.workers.intruder.consumer import (
    IntruderConsumer,
)

from services.event_processing.src.workers.zone_monitor.consumer import (
    ZoneMonitorConsumer,
)

from services.event_processing.src.workers.headcount.consumer import (
    HeadcountConsumer,
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
    headcount = HeadcountConsumer()

    try:

        await asyncio.gather(
            zone_monitor.start(),
            intruder.start(),
            headcount.start(),
        )

    except KeyboardInterrupt:

        await zone_monitor.stop()
        await intruder.stop()
        await headcount.stop()


if __name__ == "__main__":
    asyncio.run(main())