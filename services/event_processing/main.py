import asyncio
import logging
import sys

from services.event_processing.src.retention import RetentionJob
from services.event_processing.src.workers.headcount.consumer import (
    HeadcountConsumer,
)
from services.event_processing.src.workers.intruder.consumer import (
    IntruderConsumer,
)
from services.event_processing.src.workers.zone_monitor.consumer import (
    ZoneMonitorConsumer,
)
from shared.health import health_port
from shared.runner import run_consumers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


async def main() -> None:
    retention = RetentionJob()
    try:
        await run_consumers(
            "event_processing",
            [ZoneMonitorConsumer(), IntruderConsumer(), HeadcountConsumer()],
            health_port=health_port(8083),
            background={"retention": retention.run_forever},
        )
    finally:
        await retention.close()


if __name__ == "__main__":
    asyncio.run(main())
