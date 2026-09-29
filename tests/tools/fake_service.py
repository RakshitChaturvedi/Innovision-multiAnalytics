"""A stand-in service for tests of tools/platform/services.py: the REAL
shared.runner.run_consumers with the REAL /health server, an idle consumer.
FAKE_MODE=crash exits at once; FAKE_MODE=stubborn ignores the stop file."""
import asyncio
import logging
import os
import sys

from shared.runner import run_consumers


class Idle:
    last_processed_monotonic = None

    async def start(self):
        await asyncio.sleep(3600)

    async def stop(self):
        pass

    def stats(self):
        return {"name": "idle"}


async def main():
    mode = os.environ.get("FAKE_MODE", "")
    if mode == "crash":
        print("fatal: model pack missing", flush=True)
        sys.exit(3)
    if mode == "stubborn":
        os.environ.pop("INNOVISION_STOP_FILE", None)
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        await asyncio.sleep(3600)
    await run_consumers("fake", [Idle()], health_port=int(os.environ["FAKE_PORT"]),
                        redis_url=os.environ["FAKE_REDIS_URL"], database_url=os.environ["FAKE_DB_URL"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
