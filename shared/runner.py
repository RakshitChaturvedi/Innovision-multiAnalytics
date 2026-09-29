"""Run one or more consumers until SIGTERM/SIGINT or until one of them dies.

Guarantees: stop() is awaited on EVERY consumer (even if one crashed or a
stop() itself fails), and a crashed consumer makes the process exit
non-zero instead of silently leaving the service half alive.
"""

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


async def run_consumers(name: str, consumers: list) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        logger.info("shutdown_signal_received service=%s signal=%s", name, sig_name)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:
            pass  # Windows / restricted environments

    logger.info("%s_service_starting", name)
    runners = [
        asyncio.create_task(c.start(), name=f"{name}-consumer-{i}")
        for i, c in enumerate(consumers)
    ]
    waiter = asyncio.create_task(stop_event.wait(), name=f"{name}-shutdown")

    crash: BaseException | None = None
    try:
        await asyncio.wait({*runners, waiter}, return_when=asyncio.FIRST_COMPLETED)
        for r in runners:
            if r.done() and not r.cancelled() and r.exception() is not None:
                crash = r.exception()
                logger.error("%s_consumer_crashed error=%r", name, crash)
                break
    finally:
        logger.info("%s_service_stopping", name)
        waiter.cancel()

        # Graceful first: stop() lets each consumer finish in-flight work.
        # Cancelling start() first would tear that work down mid-message.
        results = await asyncio.gather(
            *(c.stop() for c in consumers), return_exceptions=True
        )
        for c, res in zip(consumers, results):
            if isinstance(res, BaseException):
                logger.error("%s_consumer_stop_failed consumer=%r error=%r", name, c, res)

        for r in runners:
            r.cancel()
        await asyncio.gather(*runners, waiter, return_exceptions=True)

        logger.info("%s_service_stopped", name)

    if crash is not None:
        raise crash
