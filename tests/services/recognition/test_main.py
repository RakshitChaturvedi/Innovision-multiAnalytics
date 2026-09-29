"""main.py: SIGTERM leads to stop() on the consumer (via shared.runner)."""
import asyncio
import os
import signal

from services.recognition import main as main_mod


class FakeConsumer:
    def __init__(self):
        self.started = asyncio.Event()
        self.stopped = False

    async def start(self):
        self.started.set()
        await asyncio.sleep(3600)

    async def stop(self):
        self.stopped = True


async def test_sigterm_stops_consumer(monkeypatch):
    fake = FakeConsumer()
    monkeypatch.setattr(main_mod, "RecognitionConsumer", lambda: fake)
    task = asyncio.create_task(main_mod.main())
    await asyncio.wait_for(fake.started.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert fake.stopped
