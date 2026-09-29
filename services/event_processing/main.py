import asyncio
import logging
import sys

from services.event_processing.src.workers.headcount.consumer import (
    HeadcountConsumer,
)
from services.event_processing.src.workers.intruder.consumer import (
    IntruderConsumer,
)
from services.event_processing.src.workers.zone_monitor.consumer import (
    ZoneMonitorConsumer,
)
from shared.runner import run_consumers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


async def main() -> None:
    await run_consumers(
        "event_processing",
        [ZoneMonitorConsumer(), IntruderConsumer(), HeadcountConsumer()],
    )


if __name__ == "__main__":
    asyncio.run(main())
