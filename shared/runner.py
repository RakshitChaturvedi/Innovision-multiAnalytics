"""Run one or more consumers until SIGTERM/SIGINT or until one of them dies.

Guarantees: stop() is awaited on EVERY consumer (even if one crashed or a
stop() itself fails), and a crashed consumer makes the process exit
non-zero instead of silently leaving the service half alive.
"""

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable

from shared.supervisor import supervise

logger = logging.getLogger(__name__)

STOP_FILE_ENV = "INNOVISION_STOP_FILE"
STOP_FILE_POLL_S = 0.5


async def _watch_stop_file(path: str, stop_event: asyncio.Event) -> None:
    """Graceful stop without signals (Windows, detached processes): the file
    appearing is a stop request (scripts/down.ps1 creates it)."""
    while not stop_event.is_set():
        if os.path.exists(path):
            logger.info("stop_file_found path=%s", path)
            stop_event.set()
            return
        await asyncio.sleep(STOP_FILE_POLL_S)


async def run_consumers(
    name: str,
    consumers: list,
    *,
    health_port: int | None = None,
    background: dict[str, Callable[[], Awaitable[None]]] | None = None,
    redis_url: str | None = None,
    database_url: str | None = None,
) -> None:
    """`health_port`: serve GET /health (shared/health.py) there.
    `background`: extra long-running jobs (e.g. retention), each run under
    shared/supervisor.supervise so a crash is logged and restarted."""
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

    stop_file = os.environ.get(STOP_FILE_ENV)
    watcher = (
        asyncio.create_task(_watch_stop_file(stop_file, stop_event), name=f"{name}-stop-file")
        if stop_file else None
    )

    logger.info("%s_service_starting", name)
    runners = [
        asyncio.create_task(c.start(), name=f"{name}-consumer-{i}")
        for i, c in enumerate(consumers)
    ]
    waiter = asyncio.create_task(stop_event.wait(), name=f"{name}-shutdown")
    jobs = [
        asyncio.create_task(supervise(job, factory, stopping=stop_event), name=f"{name}-{job}")
        for job, factory in (background or {}).items()
    ]
    close_health = None
    if health_port is not None:
        from shared.health import start_health_server

        try:
            _, close_health = await start_health_server(
                name, consumers, runners, health_port,
                redis_url=redis_url, database_url=database_url,
            )
        except BaseException:
            extra = [watcher] if watcher is not None else []
            for t in (*runners, waiter, *jobs, *extra):
                t.cancel()
            await asyncio.gather(*runners, waiter, *jobs, *extra, return_exceptions=True)
            raise

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
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        stop_event.set()  # supervised jobs: a crash from here on is shutdown
        for j in jobs:
            j.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if close_health is not None:
            try:
                await close_health()
            except Exception:
                logger.warning("%s_health_close_failed", name, exc_info=True)

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
