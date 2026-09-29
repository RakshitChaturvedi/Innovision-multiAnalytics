import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    min_backoff_s: float = 1.0,
    max_backoff_s: float = 30.0,
) -> None:
    """
    Run a long-lived background coroutine forever.

    A crash (or an unexpected clean return) is logged and the coroutine is
    restarted with exponential backoff, so a dead listener is never silent.
    Only cancellation stops it.
    """

    backoff = min_backoff_s

    while True:
        started = time.monotonic()

        try:
            await factory()
            logger.error(
                "background_task_exited name=%s restarting", name
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "background_task_crashed name=%s restarting", name
            )

        # A task that ran for a while was healthy: reset the backoff.
        if time.monotonic() - started > 60:
            backoff = min_backoff_s

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff_s)
