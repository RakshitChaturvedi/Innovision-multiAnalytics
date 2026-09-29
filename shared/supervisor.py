"""Supervisor for long-running background coroutines (pub/sub listeners,
sweepers). A dead listener must never be silent: every crash is logged and
the coroutine is restarted with exponential backoff until cancelled.

While the service is stopping (`stopping` is set) a crash or return is the
expected result of connections being closed: it is logged at DEBUG and the
coroutine is NOT restarted."""
import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    stopping: asyncio.Event | None = None,
    min_backoff: float = 1.0,
    max_backoff: float = 30.0,
    healthy_after: float = 30.0,
) -> None:
    """Run `factory()` until cancelled or until `stopping` is set.

    CancelledError is re-raised. If a run lasted at least `healthy_after`
    seconds the backoff starts over."""
    backoff = min_backoff
    while stopping is None or not stopping.is_set():
        started = time.monotonic()
        crashed = False
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            crashed = True
            if stopping is not None and stopping.is_set():
                logger.debug("background_task_stopped name=%s (crashed during shutdown)",
                             name, exc_info=True)
                return
            logger.exception("background_task_crashed name=%s; restarting in %.1fs", name, backoff)
        if not crashed:
            if stopping is not None and stopping.is_set():
                logger.debug("background_task_stopped name=%s", name)
                return
            logger.error("background_task_exited name=%s; restarting in %.1fs", name, backoff)

        if time.monotonic() - started >= healthy_after:
            backoff = min_backoff
        delay = backoff * (1 + random.random() * 0.25)
        backoff = min(backoff * 2, max_backoff)
        if stopping is None:
            await asyncio.sleep(delay)
        else:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=delay)
            except TimeoutError:
                pass
