import pytest

from services.event_processing.src.workers.zone_monitor.track_state_store import (
    TrackStateStore,
)


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.mark.asyncio
async def test_set_and_get_current_zones():
    redis = FakeRedis()

    store = TrackStateStore(redis)

    await store.set_current_zones(
        camera_id="camera-1",
        track_id=10,
        zone_ids={"zone-1", "zone-2"},
    )

    zones = await store.get_current_zones(
        camera_id="camera-1",
        track_id=10,
    )

    assert zones == {"zone-1", "zone-2"}


@pytest.mark.asyncio
async def test_missing_track_returns_empty_set():
    redis = FakeRedis()

    store = TrackStateStore(redis)

    zones = await store.get_current_zones(
        camera_id="camera-1",
        track_id=999,
    )

    assert zones == set()


@pytest.mark.asyncio
async def test_entry_time_can_be_stored_and_retrieved():
    from datetime import datetime, timezone

    redis = FakeRedis()

    store = TrackStateStore(redis)

    timestamp = datetime.now(timezone.utc)

    await store.set_entry_time(
        camera_id="camera-1",
        track_id=10,
        zone_id="zone-1",
        timestamp=timestamp,
    )

    result = await store.get_entry_time(
        camera_id="camera-1",
        track_id=10,
        zone_id="zone-1",
    )

    assert result == timestamp


@pytest.mark.asyncio
async def test_entry_time_can_be_deleted():
    from datetime import datetime, timezone

    redis = FakeRedis()

    store = TrackStateStore(redis)

    timestamp = datetime.now(timezone.utc)

    await store.set_entry_time(
        camera_id="camera-1",
        track_id=10,
        zone_id="zone-1",
        timestamp=timestamp,
    )

    await store.delete_entry_time(
        camera_id="camera-1",
        track_id=10,
        zone_id="zone-1",
    )

    result = await store.get_entry_time(
        camera_id="camera-1",
        track_id=10,
        zone_id="zone-1",
    )

    assert result is None