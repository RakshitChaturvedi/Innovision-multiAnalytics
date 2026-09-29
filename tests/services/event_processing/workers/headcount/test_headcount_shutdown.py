"""Stopping the headcount consumer against REAL Redis + REAL Postgres must be
quiet. Before the fix its ZoneStore had its own stopping event and was never
closed: stop() closed the shared Redis client under the live invalidation
listener, which logged ERROR "background_task_crashed" (ConnectionError)."""
import asyncio
import logging

import pytest

from services.event_processing.src.workers.headcount import config as hc_config
from services.event_processing.src.workers.headcount import consumer as hc
from services.event_processing.src.workers.headcount import headcount_store
from services.event_processing.src.workers.zone_monitor import config as zm_config
from services.event_processing.src.workers.zone_monitor.zone_store import (
    INVALIDATION_PATTERN,
)
from tests.redis_target import TEST_REDIS_DB, point_services_at_test_redis

@pytest.fixture
async def real_env(monkeypatch, pg_url, redis_client):
    point_services_at_test_redis(monkeypatch)
    for cfg in (hc_config.config, headcount_store.config, zm_config.config):
        monkeypatch.setattr(cfg, "DATABASE_URL", pg_url, raising=False)
    yield redis_client


async def test_stop_logs_no_error_and_stops_the_listener(real_env, caplog):
    caplog.set_level(logging.DEBUG)
    c = hc.HeadcountConsumer()
    runner = asyncio.create_task(c.start())
    for _ in range(250):
        if await real_env.pubsub_numpat() >= 1:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("invalidation listener never subscribed")
    await asyncio.sleep(0.1)
    assert c._cache.connection_pool.connection_kwargs["db"] == TEST_REDIS_DB

    await c.stop()
    runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    await asyncio.sleep(0.2)  # a restarted listener would log by now

    problems = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR or "restarting" in r.getMessage()
    ]
    assert problems == []
    assert c._zone_store._listener_task.done()
    assert await real_env.pubsub_numpat() == 0, INVALIDATION_PATTERN
