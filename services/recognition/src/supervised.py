"""Supervisor for long-running background coroutines (pub/sub listeners,
sweepers). A dead listener must never be silent: every crash is logged and the
coroutine is restarted with exponential backoff until cancelled."""
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
    min_backoff: float = 1.0,
    max_backoff: float = 30.0,
    healthy_after: float = 30.0,
) -> None:
    """Run `factory()` forever. Returns only when cancelled (CancelledError is
    re-raised). If a run lasted at least `healthy_after` seconds the backoff
    starts over."""
    backoff = min_backoff
    while True:
        started = time.monotonic()
        try:
            await factory()
            logger.warning("%s ended unexpectedly; restarting in %.1fs", name, backoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s crashed; restarting in %.1fs", name, backoff)
        if time.monotonic() - started >= healthy_after:
            backoff = min_backoff
        await asyncio.sleep(backoff * (1 + random.random() * 0.25))
        backoff = min(backoff * 2, max_backoff)
