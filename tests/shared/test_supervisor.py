"""shared.supervisor + BaseStreamConsumer._supervise: crashes are logged and
restarted, but NOT while the service is stopping."""
import asyncio
import logging

import pytest

from shared.schemas.consumer import BaseStreamConsumer
from shared.supervisor import supervise


def errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_crash_is_logged_and_restarted_while_running(caplog):
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise ConnectionError("listener died")
        await asyncio.sleep(3600)

    task = asyncio.create_task(supervise("t", flaky, stopping=asyncio.Event(),
                                         min_backoff=0.01, max_backoff=0.02))
    for _ in range(200):
        if len(runs) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(runs) == 3
    assert sum("background_task_crashed" in r.message for r in errors(caplog)) == 2


async def test_crash_while_stopping_is_quiet_and_not_restarted(caplog):
    stopping = asyncio.Event()
    runs = []

    async def listener():
        runs.append(1)
        await stopping.wait()  # the service begins to stop ...
        raise ConnectionError("connection closed by shutdown")  # ... and closes our connection

    task = asyncio.create_task(supervise("t", listener, stopping=stopping, min_backoff=0.01))
    await asyncio.sleep(0.05)
    with caplog.at_level(logging.DEBUG):
        stopping.set()
        await asyncio.wait_for(task, 1)  # returns by itself
    assert runs == [1]
    assert errors(caplog) == []


async def test_stop_during_backoff_returns_without_another_run(caplog):
    stopping = asyncio.Event()
    runs = []

    async def dies():
        runs.append(1)
        raise ConnectionError("boom")

    task = asyncio.create_task(supervise("t", dies, stopping=stopping, min_backoff=30))
    await asyncio.sleep(0.05)
    stopping.set()
    await asyncio.wait_for(task, 1)  # not after the 30 s backoff
    assert runs == [1]


class _Consumer(BaseStreamConsumer):
    async def process(self, msg_id, data, stream):
        pass


async def test_base_consumer_background_task_dying_during_stop_is_quiet(caplog):
    c = _Consumer(streams=["s"], group_name="g", consumer_name="c")
    c._running = True  # stop() has begun but in-flight work is still draining
    c._shutdown.set()
    runs = []

    async def reclaim_loop():
        runs.append(1)
        raise ConnectionError("redis closed")

    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(c._supervise("reclaim", reclaim_loop), 2)
    assert runs == [1]
    assert errors(caplog) == []


async def test_base_consumer_background_task_crash_is_still_an_error(caplog):
    c = _Consumer(streams=["s"], group_name="g", consumer_name="c")
    c._running = True
    runs = []

    async def reclaim_loop():
        runs.append(1)
        if len(runs) == 1:
            raise ConnectionError("redis blip")

    await asyncio.wait_for(c._supervise("reclaim", reclaim_loop), 5)
    assert runs == [1, 1]
    assert any("crashed; restarting" in r.message for r in errors(caplog))


@pytest.mark.parametrize("module", [
    "services.recognition.src.supervised",
    "services.event_processing.src.workers.zone_monitor.supervisor",
])
def test_service_supervisors_use_the_shared_one(module):
    import importlib

    mod = importlib.import_module(module)
    assert "stopping" in mod.supervise.__code__.co_varnames
