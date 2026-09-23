"""
Detection service entry point.

FIX: the previous version only handled KeyboardInterrupt. Under Docker
or Kubernetes the process receives SIGTERM, which raised no exception
inside asyncio.run(), so `stop()` never ran: in-flight batches were
dropped and the Redis consumer group was left with dangling pending
entries on every deploy.
"""

import asyncio
import logging
import signal
import sys

from src.config import settings
from src.consumer import DetectionConsumer

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ),
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


async def main() -> None:

    logger.info("detection_service_starting")

    consumer = DetectionConsumer()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        logger.info("shutdown_signal_received signal=%s", sig_name)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig, _request_stop, sig.name
            )
        except NotImplementedError:
            # Windows / restricted environments.
            pass

    runner = asyncio.create_task(consumer.start(), name="consumer")
    waiter = asyncio.create_task(stop_event.wait(), name="shutdown")

    try:
        done, _ = await asyncio.wait(
            {runner, waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Surface a crash in the consumer rather than exiting 0.
        if runner in done:
            runner.result()

    finally:
        logger.info("detection_service_stopping")

        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):
            pass

        waiter.cancel()

        await consumer.stop()

        logger.info("detection_service_stopped")


if __name__ == "__main__":
    asyncio.run(main())
