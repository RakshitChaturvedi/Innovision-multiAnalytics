"""EmbeddingCache loading (FAKE db) and its supervised pub/sub listener (REAL redis)."""
import asyncio
import functools
from types import SimpleNamespace

import numpy as np
import pytest

from services.recognition.src import embedding_cache as ec
from services.recognition.src.config import config
from services.recognition.src.supervised import supervise

from .conftest import FakeDB, unit


def vec_text(v) -> str:
    # what asyncpg returns for a pgvector column without a codec: text
    return "[" + ",".join(str(float(x)) for x in v) + "]"


def person_row(pid, name, embs):
    return SimpleNamespace(person_id=pid, id=pid, name=name, role="r", department="d",
                           clearance_level=2, embeddings=[vec_text(e) for e in embs])


async def wait_for(cond, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def cache():
    c = ec.EmbeddingCache()
    c._db = FakeDB()
    c._session_factory = c._db
    yield c
    await c.close()


async def test_load_two_enrolled_persons_gives_float32_matrix(cache):
    a = unit(0) * 2
    b = unit(1) + unit(2)  # two enrollment embeddings averaged
    cache._db.select_rows = [
        person_row("aaaaaaaa-0000-0000-0000-000000000001", "Ann", [a]),
        person_row("bbbbbbbb-0000-0000-0000-000000000002", "Bob", [unit(1), unit(2)]),
    ]
    await cache._load_all()

    assert cache.enrolled_matrix.dtype == np.float32
    assert cache.enrolled_matrix.shape == (2, 512)
    assert np.allclose(np.linalg.norm(cache.enrolled_matrix, axis=1), 1.0)
    assert [p["name"] for p in cache.enrolled_persons] == ["Ann", "Bob"]
    person, score = cache.search(unit(0), threshold=0.5)
    assert person["name"] == "Ann" and score == pytest.approx(1.0)
    person, _ = cache.search(b / np.linalg.norm(b), threshold=0.5)
    assert person["name"] == "Bob"


async def test_reload_replaces_matrix_even_when_everyone_was_removed(cache):
    cache._db.select_rows = [person_row("aaaaaaaa-0000-0000-0000-000000000001", "Ann", [unit(0)])]
    await cache._load_all()
    assert len(cache.enrolled_persons) == 1
    cache._db.select_rows = []
    await cache._load_all()
    assert cache.enrolled_persons == [] and cache.enrolled_matrix.shape == (0, 512)


async def test_reload_person_from_text_vectors(cache):
    cache._db.select_rows = [person_row("aaaaaaaa-0000-0000-0000-000000000001", "Ann", [unit(3)])]
    await cache.reload_person("aaaaaaaa-0000-0000-0000-000000000001")
    assert cache.enrolled_matrix.dtype == np.float32 and len(cache.enrolled_persons) == 1


# ------------------------------------------------------ supervised listener


async def test_supervise_restarts_and_logs_crashes(caplog):
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("listener died")
        await asyncio.sleep(3600)

    task = asyncio.create_task(supervise("t", flaky, min_backoff=0.01, max_backoff=0.02))
    await wait_for(lambda: len(runs) >= 3)
    assert sum("background_task_crashed" in r.message for r in caplog.records) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_listener_handles_messages_survives_connection_kill_and_stops(redis, cache, monkeypatch):
    monkeypatch.setattr(ec, "supervise", functools.partial(supervise, min_backoff=0.05, max_backoff=0.1))
    reloaded, loads = [], []
    cache.reload_person = lambda pid: _record(reloaded, pid)
    orig_load = cache._load_all
    cache._load_all = lambda: _record(loads, "all", orig_load)

    await cache.initialize(redis)
    task = cache._listener_task
    assert task is not None
    await wait_for(lambda: cache._listener_runs >= 1)

    await redis.publish(config.ENROLL_INVALIDATE_CHANNEL, "p1")
    await wait_for(lambda: reloaded == ["p1"])

    await redis.client_kill_filter(_type="pubsub")  # listener connection dies
    for _ in range(100):  # keep publishing until the (re)connected listener sees it
        await redis.publish(config.ENROLL_INVALIDATE_CHANNEL, "p2")
        if "p2" in reloaded:
            break
        await asyncio.sleep(0.1)
    assert "p2" in reloaded

    await cache.close()
    assert task.done() and cache._listener_task is None


async def test_full_reload_on_every_resubscribe(redis, cache):
    loads = []
    orig = cache._load_all
    cache._load_all = lambda: _record(loads, "all", orig)
    cache._redis = redis

    for expected_loads in (0, 1, 2):
        t = asyncio.create_task(cache._listen_invalidations())
        await wait_for(lambda n=expected_loads: cache._listener_runs >= n + 1)
        await asyncio.sleep(0.05)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        assert len(loads) == expected_loads


async def _record(bucket, value, then=None):
    bucket.append(value)
    if then is not None:
        await then()
