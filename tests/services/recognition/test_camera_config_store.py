"""CameraConfigStore: TTL, defaults for missing table/row (FAKE db), listener (REAL redis)."""
import asyncio
import functools
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import ProgrammingError

from services.recognition.src import camera_config_store as ccs
from services.recognition.src.config import config
from services.recognition.src.supervised import supervise

from .conftest import FakeDB


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def row(threshold=0.8):
    return SimpleNamespace(similarity_threshold=threshold, min_face_size_px=50,
                           blur_threshold=90.0, recognition_sample_rate=5)


@pytest.fixture
async def store():
    clock = Clock()
    s = ccs.CameraConfigStore(clock=clock)
    s._db = FakeDB()
    s._session_factory = s._db
    s.clock = clock
    yield s
    await s.close()


async def test_entries_expire_after_60s(store):
    store._db.select_rows = [row(0.8)]
    assert (await store.get("cam"))["similarity_threshold"] == 0.8
    store._db.select_rows = [row(0.9)]
    store.clock.t += 59
    assert (await store.get("cam"))["similarity_threshold"] == 0.8  # still cached
    assert store._db.select_calls == 1
    store.clock.t += 2
    assert (await store.get("cam"))["similarity_threshold"] == 0.9  # expired -> reloaded
    assert store._db.select_calls == 2


async def test_missing_row_gives_defaults_logged_once(store, caplog):
    store._db.select_rows = []
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            cfg = await store.get("cam")
            store.clock.t += 61  # force a re-query each time
    assert cfg["similarity_threshold"] == config.VISITOR_THRESHOLD
    assert cfg["recognition_sample_rate"] == config.DEFAULT_SAMPLE_RATE
    assert sum("camera_config_row_missing" in r.message for r in caplog.records) == 1


def undefined_table():
    orig = Exception('relation "camera_config" does not exist')
    orig.sqlstate = "42P01"
    return ProgrammingError("SELECT ...", {}, orig)


async def test_missing_table_gives_defaults_no_exception_logged_once(store, caplog):
    store._db.select_error = undefined_table()
    with caplog.at_level(logging.WARNING):
        for cam in ("a", "b", "c"):
            cfg = await store.get(cam)
            assert cfg["min_face_size_px"] == config.DEFAULT_MIN_FACE_SIZE_PX
    assert sum("camera_config_table_missing" in r.message for r in caplog.records) == 1
    # cached: same camera again does not hit the DB
    calls = store._db.select_calls
    await store.get("a")
    assert store._db.select_calls == calls


async def test_other_db_errors_still_raise(store):
    store._db.select_error = ProgrammingError("SELECT", {}, Exception("permission denied"))
    with pytest.raises(ProgrammingError):
        await store.get("cam")


async def test_listener_invalidates_and_is_cancelled_on_close(redis, store, monkeypatch):
    monkeypatch.setattr(ccs, "supervise", functools.partial(supervise, min_backoff=0.05, max_backoff=0.1))
    store._db.select_rows = [row(0.8)]
    await store.initialize(redis)
    task = store._listener_task
    await store.get("cam")
    assert "cam" in store._cache

    async def until_invalidated():
        for _ in range(100):
            await redis.publish(config.CAMERA_CONFIG_INVALIDATE_CHANNEL, "cam")
            if "cam" not in store._cache:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("not invalidated")

    await until_invalidated()

    # connection killed -> supervised listener comes back and works again
    await store.get("cam")
    await redis.client_kill_filter(_type="pubsub")
    await asyncio.sleep(0.2)
    await store.get("cam")
    await until_invalidated()

    await store.close()
    assert task.done()
