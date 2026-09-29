"""Kept for existing imports: the supervisor lives in shared/supervisor.py."""
import asyncio
from collections.abc import Awaitable, Callable

from shared.supervisor import supervise as _supervise


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    stopping: asyncio.Event | None = None,
    min_backoff_s: float = 1.0,
    max_backoff_s: float = 30.0,
) -> None:
    await _supervise(
        name, factory, stopping=stopping,
        min_backoff=min_backoff_s, max_backoff=max_backoff_s, healthy_after=60.0,
    )
