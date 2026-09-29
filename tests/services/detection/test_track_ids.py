"""TrackIdAllocator: unit tests on a fake counter, plus REAL Redis."""
import pytest

from services.detection.src.track_ids import TRACK_SEQ_KEY, TrackIdAllocator


class CounterRedis:
    """Fake: only INCRBY."""

    def __init__(self):
        self.value = 0
        self.fail = False

    async def incrby(self, key, n):
        if self.fail:
            raise ConnectionError("redis down")
        assert key == TRACK_SEQ_KEY
        self.value += n
        return self.value


async def test_ids_are_stable_while_track_lives_and_unique_across_cameras():
    alloc = TrackIdAllocator(CounterRedis(), track_buffer=30)
    a1 = await alloc.assign("camA", [1, 2])
    b1 = await alloc.assign("camB", [1])  # same local id, other camera
    a2 = await alloc.assign("camA", [2, 3])
    assert a2[2] == a1[2]
    assert len({a1[1], a1[2], b1[1], a2[3]}) == 4


async def test_unseen_entries_are_dropped_after_track_buffer_and_never_reused():
    alloc = TrackIdAllocator(CounterRedis(), track_buffer=3)
    first = (await alloc.assign("cam", [7]))[7]
    for _ in range(5):  # empty frames age the entry
        await alloc.assign("cam", [])
    assert alloc.size("cam") == 0
    again = (await alloc.assign("cam", [7]))[7]  # ByteTrack local id reused
    assert again != first


async def test_restart_never_reuses_ids():
    redis = CounterRedis()  # the counter outlives the process
    before = await TrackIdAllocator(redis, 30).assign("cam", [1, 2, 3])
    after = await TrackIdAllocator(redis, 30).assign("cam", [1, 2, 3])
    assert not set(before.values()) & set(after.values())


async def test_counter_failure_records_nothing_so_retry_is_clean():
    redis = CounterRedis()
    alloc = TrackIdAllocator(redis, 30)
    redis.fail = True
    with pytest.raises(ConnectionError):
        await alloc.assign("cam", [1])
    assert alloc.size("cam") == 0
    redis.fail = False
    assert await alloc.assign("cam", [1]) == {1: 1}


async def test_drop_camera_forgets_mapping():
    alloc = TrackIdAllocator(CounterRedis(), 30)
    first = (await alloc.assign("cam", [1]))[1]
    alloc.drop_camera("cam")
    assert (await alloc.assign("cam", [1]))[1] != first


async def test_real_redis_ids_unique_across_restart_and_processes(real_redis):
    one = TrackIdAllocator(real_redis, 30)
    ids1 = await one.assign("cam", [1, 2, 3])
    two = TrackIdAllocator(real_redis, 30)  # "restarted" service
    ids2 = await two.assign("cam", [1, 2, 3])
    other = await TrackIdAllocator(real_redis, 30).assign("cam2", [1])
    everything = [*ids1.values(), *ids2.values(), *other.values()]
    assert len(set(everything)) == 7
    assert int(await real_redis.get(TRACK_SEQ_KEY)) == 7
