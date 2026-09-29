"""Stopping the zone monitor against REAL Redis must be quiet: the zone
invalidation listener dying because the service closes its connections is
expected, not an ERROR, and it must not be restarted."""
import asyncio
import logging

import pytest

from services.event_processing.src.workers.zone_monitor import consumer as zm
from services.event_processing.src.workers.zone_monitor.zone_store import (
    INVALIDATION_PATTERN,
)
from tests.redis_target import TEST_REDIS_DB, point_services_at_test_redis


@pytest.fixture
async def real_redis_env(monkeypatch, redis_client):
    point_services_at_test_redis(monkeypatch)
    yield redis_client


async def _listener_subscribed(client) -> None:
    for _ in range(100):
        if await client.pubsub_numpat() >= 1:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("invalidation listener never subscribed")


def _problems(caplog):
    return [
        r for r in caplog.records
        if r.levelno >= logging.ERROR or "restarting" in r.getMessage()
    ]


async def test_stop_logs_no_error_and_does_not_restart_listener(real_redis_env, caplog):
    caplog.set_level(logging.DEBUG)
    c = zm.ZoneMonitorConsumer()
    runner = asyncio.create_task(c.start())
    await _listener_subscribed(real_redis_env)
    await asyncio.sleep(0.1)
    assert c._redis_client.connection_pool.connection_kwargs["db"] == TEST_REDIS_DB

    await c.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    await asyncio.sleep(0.2)  # a restarted listener would log by now

    assert _problems(caplog) == []
    assert c._zone_store._listener_task.done()
    assert await real_redis_env.pubsub_numpat() == 0, INVALIDATION_PATTERN


@pytest.mark.parametrize("yields", [0, 1, 3, 10])
async def test_stop_while_starting_is_not_lost(real_redis_env, caplog, yields):
    """stop() can arrive before start() reached BaseStreamConsumer.start(),
    which used to clear the stopping event: the consumer then ran on after
    stop() and a later listener death was logged as ERROR and restarted."""
    caplog.set_level(logging.DEBUG)
    c = zm.ZoneMonitorConsumer()
    runner = asyncio.create_task(c.start())
    for _ in range(yields):
        await asyncio.sleep(0)

    await c.stop()
    await asyncio.wait_for(runner, timeout=5)  # start() returns: it was stopped

    assert c._shutdown.is_set()
    await asyncio.sleep(0.2)
    assert await real_redis_env.pubsub_numpat() == 0
    assert _problems(caplog) == []
