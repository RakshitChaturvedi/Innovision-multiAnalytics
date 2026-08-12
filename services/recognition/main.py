import asyncio
import logging
import sys

from src.consumer import RecognitionConsumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("recognition_main")


async def main() -> None:
    logger.info("recognition_service_starting")
    consumer = RecognitionConsumer()
    try:
        await consumer.start()
    except KeyboardInterrupt:
        logger.info("recognition_service_stopping")
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
