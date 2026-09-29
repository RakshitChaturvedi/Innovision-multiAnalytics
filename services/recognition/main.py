import asyncio
import logging
import sys

from services.recognition.src.consumer import RecognitionConsumer
from shared.runner import run_consumers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


async def main() -> None:
    await run_consumers("recognition", [RecognitionConsumer()])


if __name__ == "__main__":
    asyncio.run(main())
