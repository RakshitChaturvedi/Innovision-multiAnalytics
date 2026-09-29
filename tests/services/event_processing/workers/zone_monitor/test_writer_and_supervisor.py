"""Fakes only (no DB/Redis): SQL text, call ordering, supervisor restarts."""
import asyncio
import logging

import pytest

from services.event_processing.src.workers.zone_monitor.event_writer import (
    ZoneEventWriter,
)
from services.event_processing.src.workers.zone_monitor.processor import (
    _make_event,
)
from services.event_processing.src.workers.zone_monitor.supervisor import (
    supervise,
)
from shared.schemas.enums import EventType

from .conftest import CAMERA, T0, ZONE


class Trace:
    def __init__(self):
        self.calls, self.params, self.sql = [], None, None


class FakeSession:
    def __init__(self, trace):
        self.t = trace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return self

    async def execute(self, stmt, params):
        self.t.calls.append("insert")
        self.t.sql, self.t.params = str(stmt), params


class FakeRedis:
    def __init__(self, trace, fail=False):
        self.t, self.fail = trace, fail

    async def xadd(self, stream, fields, **kw):
        self.t.calls.append("xadd")
        if self.fail:
            raise ConnectionError("down")


def event():
    return _make_event(
        str(CAMERA), 1, str(ZONE), EventType.ENTERED, 7, T0, None, "frame:c:7"
    )


async def test_insert_is_idempotent_stores_frame_ref_and_precedes_xadd():
    t = Trace()
    await ZoneEventWriter(lambda: FakeSession(t), FakeRedis(t)).write(event())

    assert t.calls == ["insert", "xadd"]
    assert "ON CONFLICT DO NOTHING" in t.sql
    assert t.params["frame_reference"] == "frame:c:7"


async def test_xadd_failure_raises():
    t = Trace()
    with pytest.raises(ConnectionError):
        await ZoneEventWriter(
            lambda: FakeSession(t), FakeRedis(t, fail=True)
        ).write(event())


async def test_supervisor_restarts_and_logs_crash(caplog):
    runs = 0

    async def flaky():
        nonlocal runs
        runs += 1
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(
            supervise("t", flaky, min_backoff_s=0.01, max_backoff_s=0.02)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert runs >= 3
    assert any("background_task_crashed" in r.message for r in caplog.records)
