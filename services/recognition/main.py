import asyncio
import logging
import sys

from services.recognition.src.consumer import RecognitionConsumer
from shared.health import health_port
from shared.runner import run_consumers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


async def main() -> None:
    await run_consumers(
        "recognition", [RecognitionConsumer()], health_port=health_port(8082)
    )


if __name__ == "__main__":
    asyncio.run(main())
