"""run_consumers: stop() is awaited on every consumer; crashes are not silent. FAKES."""
import asyncio
import os
import signal

import pytest

from shared.runner import run_consumers


class Fake:
    def __init__(self, crash=None, stop_error=None):
        self.crash, self.stop_error = crash, stop_error
        self.stopped = False

    async def start(self):
        if self.crash:
            raise self.crash
        await asyncio.sleep(3600)

    async def stop(self):
        self.stopped = True
        if self.stop_error:
            raise self.stop_error


async def test_sigterm_stops_every_consumer():
    cs = [Fake(), Fake(), Fake()]
    task = asyncio.create_task(run_consumers("t", cs))
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert all(c.stopped for c in cs)


async def test_crash_is_raised_and_all_consumers_still_stopped():
    cs = [Fake(), Fake(crash=RuntimeError("boom")), Fake()]
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(run_consumers("t", cs), 5)
    assert all(c.stopped for c in cs)


async def test_failing_stop_does_not_skip_the_others():
    cs = [Fake(stop_error=ValueError("x")), Fake(crash=RuntimeError("boom")), Fake()]
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(run_consumers("t", cs), 5)
    assert all(c.stopped for c in cs)


async def test_stop_file_stops_every_consumer(tmp_path, monkeypatch):
    """Windows has no SIGTERM for a detached process: scripts/down.ps1 creates
    INNOVISION_STOP_FILE and the service must stop gracefully by itself."""
    stop = tmp_path / "svc.stop"
    monkeypatch.setenv("INNOVISION_STOP_FILE", str(stop))
    cs = [Fake(), Fake()]
    task = asyncio.create_task(run_consumers("t", cs))
    await asyncio.sleep(0.1)
    assert not task.done()
    stop.write_text("stop")
    await asyncio.wait_for(task, 5)
    assert all(c.stopped for c in cs)
