"""BaseStreamConsumer against a REAL local redis-server (see conftest.py)."""
import asyncio
import json
import logging
import random
import time
from uuid import uuid4

import pytest
from redis.exceptions import ResponseError

from shared.errors import PermanentError
from shared.schemas.consumer import BaseStreamConsumer

STREAM = "events:test"
GROUP = "test_group"


@pytest.fixture(autouse=True)
def point_settings_at_test_redis(monkeypatch, redis_port):
    from shared.config import settings
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", redis_port)


class Probe(BaseStreamConsumer):
    """Consumer whose behaviour is a plain async callable."""

    def __init__(self, handler, *, name="c1", streams=None, **kw):
        defaults = dict(block_ms=50, reclaim_idle_ms=100, reclaim_interval_s=0.1)
        defaults.update(kw)
        super().__init__(streams=streams or [STREAM], group_name=GROUP,
                         consumer_name=name, **defaults)
        self.handler = handler
        self.calls: list[tuple[str, str]] = []  # (stream, msg_id)

    async def process(self, msg_id, data, stream):
        self.calls.append((stream, msg_id))
        await self.handler(self, msg_id, data, stream)


async def ok(c, msg_id, data, stream):
    return None


def payload(camera="cam", seq=0):
    return {"data": json.dumps({"camera_id": camera, "seq": seq})}


async def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def pending(redis, stream=STREAM):
    return (await redis.xpending(stream, GROUP))["pending"]


class Running:
    """async context manager: start the consumer, always stop it."""

    def __init__(self, consumer):
        self.c = consumer

    async def __aenter__(self):
        self.task = asyncio.create_task(self.c.start())
        await asyncio.sleep(0.05)
        return self.c

    async def __aexit__(self, *exc):
        await self.c.stop()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)


# ---------------------------------------------------------------- required


async def test_failing_message_is_retried_after_idle_and_succeeds_second_time(redis):
    attempts = {}

    async def flaky(c, msg_id, data, stream):
        attempts[msg_id] = attempts.get(msg_id, 0) + 1
        if attempts[msg_id] == 1:
            raise RuntimeError("transient")

    mid = await redis.xadd(STREAM, payload())
    async with Running(Probe(flaky)) as c:
        await wait_until(lambda: c.stats()["processed"] == 1)
        assert attempts[mid.decode()] == 2
        assert await pending(redis) == 0
        assert c.stats()["failed"] == 1
        assert c.stats()["reclaimed"] >= 1
        assert c.stats()["dlq"] == 0
        assert await redis.xlen(f"{STREAM}:dlq") == 0


async def test_message_failing_past_max_deliveries_goes_to_dlq_and_is_acked(redis):
    async def always_fails(c, msg_id, data, stream):
        raise RuntimeError("boom")

    mid = await redis.xadd(STREAM, payload("camX", 7))
    async with Running(Probe(always_fails, max_deliveries=2)) as c:
        await wait_until(lambda: c.stats()["dlq"] == 1)
        # deliveries 1 and 2 are processed; the 3rd claim exceeds the limit
        assert len(c.calls) == 2
        assert await pending(redis) == 0

    (dlq_id, fields), = await redis.xrange(f"{STREAM}:dlq")
    assert json.loads(fields[b"data"]) == {"camera_id": "camX", "seq": 7}
    assert b"max_deliveries" in fields[b"error"]
    assert fields[b"group"] == GROUP.encode()
    assert fields[b"msg_id"] == mid


async def test_permanent_error_goes_to_dlq_immediately(redis):
    async def permanent(c, msg_id, data, stream):
        raise PermanentError("frame expired")

    mid = await redis.xadd(STREAM, payload())
    async with Running(Probe(permanent, reclaim_idle_ms=60000)) as c:
        await wait_until(lambda: c.stats()["dlq"] == 1)
        assert len(c.calls) == 1  # never retried
        assert await pending(redis) == 0
        assert c.stats()["failed"] == 0

    (_, fields), = await redis.xrange(f"{STREAM}:dlq")
    assert b"PermanentError: frame expired" in fields[b"error"]
    assert fields[b"msg_id"] == mid


async def test_per_camera_order_is_exact_while_other_camera_runs_concurrently(redis):
    rng = random.Random(1234)
    spans = {"A": [], "B": []}

    async def record(c, msg_id, data, stream):
        body = json.loads(data[b"data"])
        cam, seq = body["camera_id"], body["seq"]
        start = time.monotonic()
        await asyncio.sleep(rng.uniform(0, 0.02) if cam == "A" else 0.005)
        spans[cam].append((seq, start, time.monotonic()))

    for i in range(20):
        await redis.xadd(STREAM, payload("A", i))
        await redis.xadd(STREAM, payload("B", i))

    # small batches so ordering must also hold across batches
    async with Running(Probe(record, batch_size=7, reclaim_idle_ms=60000)) as c:
        await wait_until(lambda: c.stats()["processed"] == 40, timeout=10)

    assert [s for s, _, _ in spans["A"]] == list(range(20))
    assert [s for s, _, _ in spans["B"]] == list(range(20))
    a = spans["A"]
    assert all(a[i][1] >= a[i - 1][2] for i in range(1, 20)), "same camera overlapped"
    overlap = any(bs < ae and be > as_
                  for _, as_, ae in spans["A"] for _, bs, be in spans["B"])
    assert overlap, "different cameras did not run concurrently"


async def test_own_pending_entries_are_processed_after_crash_and_restart(redis):
    ids = [await redis.xadd(STREAM, payload("cam", i)) for i in range(3)]

    async def hang(c, msg_id, data, stream):
        await asyncio.sleep(3600)

    first = Probe(hang, name="worker-1", reclaim_idle_ms=60000)
    task = asyncio.create_task(first.start())
    await wait_until(lambda: len(first.calls) == 1)  # one camera -> sequential
    # kill -9 equivalent: no stop(), no acks
    task.cancel()
    for t in list(first._inflight) + first._bg_tasks:
        t.cancel()
    await asyncio.gather(task, *first._inflight, *first._bg_tasks, return_exceptions=True)
    await first.redis.aclose()
    assert await pending(redis) == 3

    # restart with the same consumer name; reclaim idle is huge so only the
    # startup drain of own pending entries can explain the result
    seen = []

    async def record(c, msg_id, data, stream):
        seen.append(msg_id)

    async with Running(Probe(record, name="worker-1", reclaim_idle_ms=60000)) as c:
        await wait_until(lambda: c.stats()["processed"] == 3)
        assert seen == [i.decode() for i in ids]
        assert await pending(redis) == 0
        assert c.stats()["reclaimed"] == 0


# ------------------------------------------------------------------- extra


async def test_busygroup_is_ignored_but_other_group_errors_raise(redis):
    await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    async with Running(Probe(ok)):  # group already exists -> fine
        pass

    await redis.set("notastream", "x")
    bad = Probe(ok, streams=["notastream"])
    with pytest.raises(ResponseError, match="WRONGTYPE"):
        await asyncio.wait_for(bad.start(), 5)
    await bad.stop()


async def test_auto_ack_false_leaves_pending_until_explicit_ack(redis):
    class Manual(Probe):
        auto_ack = False

    mid = await redis.xadd(STREAM, payload())
    async with Running(Manual(ok, reclaim_idle_ms=60000)) as c:
        await wait_until(lambda: len(c.calls) == 1)
        await asyncio.sleep(0.1)
        assert await pending(redis) == 1
        assert c.stats()["processed"] == 0
        await c.ack(STREAM, mid)
        assert await pending(redis) == 0
        assert c.stats()["processed"] == 1


async def test_add_and_remove_stream_at_runtime(redis):
    s1, s2 = "events:one", "events:two"
    async with Running(Probe(ok, streams=[s1], reclaim_idle_ms=60000)) as c:
        await c.add_stream(s2)
        await redis.xadd(s2, payload())
        await wait_until(lambda: (s2, ) == tuple(x[0] for x in c.calls[-1:]))

        await c.remove_stream(s1)
        await asyncio.sleep(0.1)  # let an in-progress blocking read finish
        before = len(c.calls)
        await redis.xadd(s1, payload())
        await asyncio.sleep(0.3)
        assert len(c.calls) == before
        assert c.stats()["streams"] == [s2]


async def test_stop_waits_for_in_flight_work(redis):
    async def slow(c, msg_id, data, stream):
        await asyncio.sleep(0.4)

    await redis.xadd(STREAM, payload())
    c = Probe(slow, reclaim_idle_ms=60000)
    task = asyncio.create_task(c.start())
    await wait_until(lambda: len(c.calls) == 1)
    t0 = time.monotonic()
    await c.stop()
    assert time.monotonic() - t0 >= 0.25
    assert c.stats()["processed"] == 1
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert await pending(redis) == 0


async def test_trimmed_pending_entry_is_acked_with_warning(redis, caplog):
    await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    mid = await redis.xadd(STREAM, payload())
    await redis.xreadgroup(GROUP, "ghost", {STREAM: ">"}, count=1)  # now pending for "ghost"
    await redis.xdel(STREAM, mid)  # payload gone, PEL entry remains

    with caplog.at_level(logging.WARNING):
        async with Running(Probe(ok)) as c:
            await wait_until(lambda: c.stats()["lost"] == 1)
            assert await pending(redis) == 0
            assert c.calls == []
    assert any("trimmed" in r.getMessage() for r in caplog.records)


async def test_recovers_when_redis_loses_the_group(redis):
    seen = []

    async def record(c, msg_id, data, stream):
        seen.append(json.loads(data[b"data"])["seq"])

    async with Running(Probe(record, reclaim_idle_ms=60000)) as c:
        await redis.xadd(STREAM, payload("cam", 1))
        await wait_until(lambda: 1 in seen)

        await redis.xgroup_destroy(STREAM, GROUP)  # like a Redis restart w/o persistence
        await redis.xadd(STREAM, payload("cam", 2))
        # The group is recreated at id 0, so earlier entries may be replayed
        # (processing is idempotent by rule); what matters is nothing is lost.
        await wait_until(lambda: 2 in seen, timeout=8)
        await wait_until(lambda: c.stats()["running"])


async def test_exception_in_one_message_does_not_block_others(redis):
    async def fail_first(c, msg_id, data, stream):
        if json.loads(data[b"data"])["seq"] == 0:
            raise RuntimeError("bad")

    for i in range(3):
        await redis.xadd(STREAM, payload("cam", i))
    async with Running(Probe(fail_first, reclaim_idle_ms=60000)) as c:
        await wait_until(lambda: c.stats()["processed"] == 2)
        assert c.stats()["failed"] == 1
        assert await pending(redis) == 1


def test_partition_key_defaults_to_camera_id_then_stream():
    c = Probe(ok)
    cam = str(uuid4())
    assert c.partition_key("s", {b"data": json.dumps({"camera_id": cam}).encode()}) == cam
    assert c.partition_key("s", {"data": json.dumps({"camera_id": cam})}) == cam
    assert c.partition_key("s", {b"data": b"not json"}) == "s"
    assert c.partition_key("s", {b"data": b"[1, 2]"}) == "s"
    assert c.partition_key("s", {b"other": b"x"}) == "s"
