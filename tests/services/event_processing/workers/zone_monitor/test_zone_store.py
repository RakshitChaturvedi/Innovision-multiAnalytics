"""ZoneStore against a fake DB session and REAL Redis (skipped if unreachable)."""
import asyncio
import logging
from types import SimpleNamespace

from services.event_processing.src.workers.zone_monitor.zone_store import (
    ZoneStore,
    publish_zone_invalidation,
    validate_polygon,
)

SQUARE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]


class FakeSession:
    def __init__(self, rows, counter):
        self.rows, self.counter = rows, counter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        self.counter["n"] += 1
        return SimpleNamespace(fetchall=lambda: self.rows)


def row(zone_id, polygon):
    return SimpleNamespace(
        id=zone_id, name="n", type="restricted", polygon=polygon,
        max_headcount=None, dwell_threshold_seconds=None,
    )


def make_store(rows, clock=None):
    counter = {"n": 0}
    kwargs = {"clock": clock} if clock else {}
    store = ZoneStore(session_factory=lambda: FakeSession(rows, counter), **kwargs)
    return store, counter


def test_validate_polygon():
    assert validate_polygon(SQUARE) == SQUARE
    assert validate_polygon([{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}]) == [
        [0.1, 0.1], [0.9, 0.1], [0.5, 0.9]
    ]
    assert validate_polygon('[[0,0],[1,0],[1,1]]') == [[0, 0], [1, 0], [1, 1]]
    assert validate_polygon([[0.1, 0.1], [0.9, 0.1]]) is None  # < 3 points
    assert validate_polygon([[0.1, 0.1], [0.9, 0.1], [1.5, 0.5]]) is None  # out of bounds
    assert validate_polygon([[0.1, 0.1], [0.9, 0.1], [-0.1, 0.5]]) is None
    assert validate_polygon([[0.1, 0.1], [0.9, 0.1], [0.5]]) is None  # bad point
    assert validate_polygon([[0.1, 0.1], [0.9, 0.1], ["a", "b"]]) is None
    assert validate_polygon(None) is None


async def test_out_of_bounds_polygon_ignored_with_one_warning(redis_client, caplog):
    rows = [row("good", SQUARE), row("bad", [[0, 0], [2, 0], [1, 1]])]
    store, _ = make_store(rows, clock=iter(range(0, 10_000, 100)).__next__)
    store._redis = redis_client

    with caplog.at_level(logging.WARNING):
        zones = await store.get_zones("cam")
        await store.get_zones("cam")  # TTL expired -> reload -> no second warning
        await redis_client.flushdb()
        await store.get_zones("cam")

    assert [z["id"] for z in zones] == ["good"]
    warnings = [r for r in caplog.records if "zone_invalid_polygon_skipped" in r.message]
    assert len(warnings) == 1


async def test_local_cache_ttl(redis_client):
    now = {"t": 0.0}
    store, counter = make_store([row("z", SQUARE)], clock=lambda: now["t"])
    store._redis = redis_client

    await store.get_zones("cam")
    await redis_client.flushdb()  # hide L2 so a reload must hit the DB
    await store.get_zones("cam")
    assert counter["n"] == 1  # served from L1

    now["t"] = 31.0  # past the 30 s TTL
    await store.get_zones("cam")
    assert counter["n"] == 2


async def test_invalidation_message_drops_cache(redis_client):
    store, counter = make_store([row("z", SQUARE)])
    await store.initialize(redis_client)
    try:
        await asyncio.sleep(0.2)  # let the listener subscribe
        await store.get_zones("cam")
        await publish_zone_invalidation(redis_client, "cam")
        await asyncio.sleep(0.2)
        await store.get_zones("cam")
        assert counter["n"] == 2
    finally:
        await store.close()
