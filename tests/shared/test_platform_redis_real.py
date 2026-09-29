"""fetch_frame + AlertPublisher against a REAL local redis-server.

Skipped locally without the redis-server binary (fails in CI).
"""
from uuid import uuid4

import pytest

from shared.alerting.publisher import AlertPublisher, build_platform_alert
from shared.frames import FrameUnavailable, fetch_frame
from shared.platform_contracts.alert_event import AlertEvent
from shared.platform_contracts.enums import FrameProvider

# The `redis` fixture starts a throwaway redis-server (skips without the
# binary locally, fails under REQUIRE_REAL_SERVICES=1).


async def test_real_redis_fetch_verbatim_key(redis):
    cam = uuid4()
    await redis.set(f"frame:{cam}:42", b"jpg")
    await redis.set(f"frames:frame:{cam}:42", b"WRONG")  # prefixed key must never be read
    got = await fetch_frame(redis, None, camera_id=cam, frame_seq=42,
                            frame_reference=f"frame:{cam}:42",
                            frame_provider=FrameProvider.REDIS)
    assert got == b"jpg"


async def test_real_redis_miss_without_minio_raises(redis):
    with pytest.raises(FrameUnavailable):
        await fetch_frame(redis, None, camera_id=uuid4(), frame_seq=1,
                          frame_reference="frame:none:1",
                          frame_provider=FrameProvider.REDIS)


async def test_real_redis_publish_alert(redis):
    alert = build_platform_alert(
        domain_event_id=uuid4(), camera_id=uuid4(),
        timestamp=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        severity="high", alert_type="headcount_breach", title="t",
        description=None, frame_seq=3, metadata={})
    publisher = AlertPublisher(redis)
    await publisher.publish(alert)
    await publisher.publish(alert)  # retry: same alert_id both times
    entries = await redis.xrange("alerts:live")
    assert len(entries) == 2
    ids = {AlertEvent.model_validate_json(f[b"data"]).alert_id for _, f in entries}
    assert ids == {alert.alert_id}
