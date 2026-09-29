"""Headcount consumer tests. Run against FAKES (in-memory repo/cache/publisher);
no Redis or Postgres is involved."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import RedisError

from services.event_processing.src.workers.headcount.config import config
from services.event_processing.src.workers.headcount.consumer import (
    HeadcountConsumer,
    InvalidPayload,
)
from services.event_processing.src.workers.headcount.headcount_store import OpenBreach
from shared.alerting.publisher import build_platform_alert
from shared.errors import PermanentError
from shared.frames import minio_frame_key
from shared.platform_contracts.alert_event import AlertEvent
from shared.platform_contracts.enums import AlertSeverity, FrameProvider
from shared.schemas.events import DetectionEvent

def alert_id_for(breach_id):
    """Expected deterministic id: what the shared builder derives from the breach id."""
    return build_platform_alert(
        domain_event_id=uuid.UUID(breach_id), camera_id=uuid.UUID(CAM), timestamp=T0,
        severity=AlertSeverity.HIGH, alert_type="headcount_breach",
        title="t", description="d", frame_seq=None,
    ).alert_id


T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
CAM = str(uuid.uuid4())
ZONE = str(uuid.uuid4())
SQUARE = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


class FakeRepo:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.snapshots: list[tuple[str, datetime]] = []
        self.fail_resolve = 0

    def open_rows(self):
        return [r for r in self.rows.values() if r["status"] == "open"]

    async def load_open_breaches(self):
        return [
            OpenBreach(i, r["zone_id"], r["camera_id"], r["alert_published"])
            for i, r in self.rows.items()
            if r["status"] == "open"
        ]

    async def open_breach(self, zone_id, camera_id, count, threshold, timestamp):
        for i, r in self.rows.items():  # partial unique index emulation
            if r["zone_id"] == zone_id and r["status"] == "open":
                return OpenBreach(i, zone_id, camera_id, r["alert_published"])
        i = str(uuid.uuid4())
        self.rows[i] = dict(
            zone_id=zone_id, camera_id=camera_id, status="open",
            alert_published=False, reason=None, resolved_at=None,
        )
        return OpenBreach(i, zone_id, camera_id, False)

    async def mark_alert_published(self, breach_id, alert_id):
        self.rows[breach_id]["alert_published"] = True
        self.rows[breach_id]["alert_id"] = alert_id

    async def resolve_breach(self, breach_id, timestamp, reason):
        if self.fail_resolve:
            self.fail_resolve -= 1
            raise ConnectionError("db down")
        r = self.rows[breach_id]
        r.update(status="resolved", reason=reason, resolved_at=timestamp)

    async def write_snapshot(self, camera_id, zone_id, count, rolling_avg, timestamp):
        self.snapshots.append((zone_id, timestamp))


class FakeCache:
    def __init__(self):
        self.store: dict[str, tuple[str, int]] = {}
        self.fail = False

    async def set(self, key, value, ex=None):
        if self.fail:
            raise RedisError("boom")
        self.store[key] = (value, ex)

    async def delete(self, key):
        self.store.pop(key, None)


class FakeAlerts:
    def __init__(self):
        self.sent: list[AlertEvent] = []
        self.fail = 0

    async def publish(self, alert):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("redis down")
        self.sent.append(alert)


class FakeZones:
    def __init__(self, max_headcount=10):
        self.zones = [
            {"id": ZONE, "name": "Lobby", "polygon": SQUARE, "max_headcount": max_headcount}
        ]

    async def get_zones(self, camera_id):
        return self.zones if camera_id == CAM else []


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_cfg(**over):
    cfg = copy.copy(config)
    cfg.ROLLING_WINDOW_SECONDS = 2  # short window so the average follows counts
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def make_consumer(repo=None, cache=None, alerts=None, zones=None, clock=None, **cfg):
    repo = repo or FakeRepo()
    c = HeadcountConsumer(
        cfg=make_cfg(**cfg),
        zone_store=zones or FakeZones(),
        repo=repo,
        cache=cache or FakeCache(),
        alert_publisher=alerts or FakeAlerts(),
        clock=clock or Clock(),
    )
    return c, repo


def payload(t, people, seq=0):
    tracks = [
        dict(track_id=i, bbox=dict(x1=0.4, y1=0.4, x2=0.6, y2=0.6),
             confidence=0.9, class_label="person")
        for i in range(people)
    ]
    ev = DetectionEvent(
        camera_id=CAM, frame_event_id=uuid.uuid4(),
        timestamp=T0 + timedelta(seconds=t), frame_reference=f"frame:{CAM}:{seq}",
        frame_seq=seq, frame_shape=(480, 640), tracks=tracks,
        frame_provider=FrameProvider.REDIS, inference_latency_ms=1.0,
    )
    return {"data": ev.model_dump_json()}


async def feed(consumer, series):
    """series: iterable of (t_seconds, people)."""
    for n, (t, people) in enumerate(series):
        await consumer.process(f"{n}-0", payload(t, people, seq=int(t)), "events:detections")


async def test_oscillation_around_max_never_floods_alerts():
    alerts = FakeAlerts()
    c, repo = make_consumer(alerts=alerts)
    await feed(c, [(t, 10 if t % 2 == 0 else 11) for t in range(120)])
    assert len(alerts.sent) <= 1
    assert len(repo.rows) <= 1


async def test_sustained_breach_alerts_exactly_once():
    alerts = FakeAlerts()
    c, repo = make_consumer(alerts=alerts)
    await feed(c, [(t, 12) for t in range(0, 5)])
    assert alerts.sent == []  # 4 s < BREACH_ENTER_SECONDS
    await feed(c, [(t, 12) for t in range(5, 30)])
    assert len(alerts.sent) == 1
    alert = alerts.sent[0]
    (breach_id,) = repo.rows
    assert alert.alert_id == alert_id_for(breach_id)
    assert str(alert.source_event_id) == breach_id
    assert alert.alert_type == "headcount_breach"
    assert alert.frame_provider == FrameProvider.MINIO
    assert alert.frame_reference == minio_frame_key(uuid.UUID(CAM), 5)
    assert alert.metadata["threshold"] == 10
    assert alert.metadata["zone_id"] == ZONE and alert.metadata["zone_name"] == "Lobby"
    assert repo.rows[breach_id]["alert_published"] is True
    assert repo.rows[breach_id]["alert_id"] == str(alert.alert_id)

async def test_drop_below_threshold_resolves_after_exit_seconds():
    alerts = FakeAlerts()
    c, repo = make_consumer(alerts=alerts)
    await feed(c, [(t, 12) for t in range(0, 8)])
    (breach_id,) = repo.rows
    await feed(c, [(t, 8) for t in range(8, 8 + 10)])  # 9 s of 8 (<10 s hold)
    assert repo.rows[breach_id]["status"] == "open"
    await feed(c, [(t, 8) for t in range(18, 22)])
    r = repo.rows[breach_id]
    assert r["status"] == "resolved" and r["reason"] == "below_threshold"
    assert len(alerts.sent) == 1


async def test_restart_with_open_published_breach_does_not_realert():
    repo, alerts = FakeRepo(), FakeAlerts()
    c1, _ = make_consumer(repo=repo, alerts=alerts)
    await feed(c1, [(t, 12) for t in range(0, 10)])
    assert len(alerts.sent) == 1

    c2, _ = make_consumer(repo=repo, alerts=alerts)  # "restart": fresh memory
    await c2.load_state()
    await feed(c2, [(t, 12) for t in range(100, 130)])
    assert len(alerts.sent) == 1
    assert len(repo.rows) == 1


async def test_restart_without_load_still_dedupes_via_unique_index():
    repo, alerts = FakeRepo(), FakeAlerts()
    c1, _ = make_consumer(repo=repo, alerts=alerts)
    await feed(c1, [(t, 12) for t in range(0, 10)])
    c2, _ = make_consumer(repo=repo, alerts=alerts)  # forgot load_state
    await feed(c2, [(t, 12) for t in range(100, 130)])
    assert len(alerts.sent) == 1
    assert len(repo.rows) == 1


async def test_restart_republishes_breach_whose_alert_was_lost_once():
    repo, alerts = FakeRepo(), FakeAlerts()
    c1, _ = make_consumer(repo=repo, alerts=alerts)
    alerts.fail = 1
    with pytest.raises(ConnectionError):
        await feed(c1, [(t, 12) for t in range(0, 10)])
    (breach_id,) = repo.rows
    assert repo.rows[breach_id]["alert_published"] is False

    c2, _ = make_consumer(repo=repo, alerts=alerts)
    await c2.load_state()
    await feed(c2, [(t, 12) for t in range(100, 110)])
    assert len(alerts.sent) == 1
    assert alerts.sent[0].alert_id == alert_id_for(breach_id)
    assert repo.rows[breach_id]["alert_published"] is True


async def test_publish_failure_retry_of_same_message_publishes_once():
    repo, alerts = FakeRepo(), FakeAlerts()
    c, _ = make_consumer(repo=repo, alerts=alerts)
    await feed(c, [(t, 12) for t in range(0, 5)])
    alerts.fail = 1
    msg = payload(5, 12, seq=5)
    with pytest.raises(ConnectionError):
        await c.process("m", msg, "events:detections")
    await c.process("m", msg, "events:detections")  # redelivery
    await feed(c, [(t, 12) for t in range(6, 20)])
    assert len(alerts.sent) == 1
    assert len(repo.rows) == 1


async def test_resolve_failure_retry_of_same_message_resolves():
    repo = FakeRepo()
    c, _ = make_consumer(repo=repo)
    await feed(c, [(t, 12) for t in range(0, 8)])
    (breach_id,) = repo.rows
    await feed(c, [(t, 5) for t in range(8, 19)])
    repo.fail_resolve = 1
    msg = payload(19, 5, seq=19)
    with pytest.raises(ConnectionError):
        await c.process("m", msg, "events:detections")
    assert repo.rows[breach_id]["status"] == "open"
    await c.process("m", msg, "events:detections")
    assert repo.rows[breach_id]["status"] == "resolved"


async def test_stale_camera_resolves_and_drops_state():
    repo, cache, clock = FakeRepo(), FakeCache(), Clock()
    c, _ = make_consumer(repo=repo, cache=cache, clock=clock)
    await feed(c, [(t, 12) for t in range(0, 8)])
    (breach_id,) = repo.rows
    assert f"headcount:current:{ZONE}" in cache.store

    clock.t += 29
    assert await c.sweep_stale() == []
    assert repo.rows[breach_id]["status"] == "open"

    clock.t += 2  # 31 s of wall clock without events
    assert await c.sweep_stale() == [CAM]
    r = repo.rows[breach_id]
    assert r["status"] == "resolved" and r["reason"] == "camera_stale"
    assert f"headcount:current:{ZONE}" not in cache.store
    assert c._zones == {} and c._camera_zones == {}
    assert c.metrics["resolved_stale"] == 1


async def test_stale_sweep_covers_breach_loaded_at_startup():
    repo, clock = FakeRepo(), Clock()
    c1, _ = make_consumer(repo=repo)
    await feed(c1, [(t, 12) for t in range(0, 8)])
    (breach_id,) = repo.rows

    c2, _ = make_consumer(repo=repo, clock=clock)
    await c2.load_state()
    clock.t += 31
    await c2.sweep_stale()
    assert repo.rows[breach_id]["reason"] == "camera_stale"


async def test_current_key_written_with_ttl():
    cache = FakeCache()
    c, _ = make_consumer(cache=cache)
    await feed(c, [(0, 3), (1, 5)])
    value, ttl = cache.store[f"headcount:current:{ZONE}"]
    body = json.loads(value)
    assert body["count"] == 5 and body["rolling_avg"] == 4.0
    assert body["ts"] == (T0 + timedelta(seconds=1)).isoformat()
    assert ttl == 60


async def test_cache_failure_is_counted_and_does_not_block_alerts():
    cache, alerts = FakeCache(), FakeAlerts()
    cache.fail = True
    c, _ = make_consumer(cache=cache, alerts=alerts)
    await feed(c, [(t, 12) for t in range(0, 10)])
    assert len(alerts.sent) == 1
    assert c.metrics["current_cache_errors"] > 0


async def test_snapshots_use_event_time():
    c, repo = make_consumer(SNAPSHOT_INTERVAL_SECONDS=5)
    # 20 frames spread over 20 event-seconds, processed instantly
    await feed(c, [(t, 3) for t in range(21)])
    assert [ts for _, ts in repo.snapshots] == [
        T0 + timedelta(seconds=s) for s in (0, 5, 10, 15, 20)
    ]


async def test_no_threshold_zone_is_ignored():
    alerts = FakeAlerts()
    c, repo = make_consumer(alerts=alerts, zones=FakeZones(max_headcount=None))
    await feed(c, [(t, 50) for t in range(20)])
    assert alerts.sent == [] and repo.rows == {} and repo.snapshots == []


@pytest.mark.parametrize("data", [{}, {"data": "not json"}, {"data": b"{}"}])
async def test_invalid_payload_raises_and_is_counted(data):
    c, _ = make_consumer()
    with pytest.raises(InvalidPayload) as ei:
        await c.process("bad", data, "events:detections")
    assert isinstance(ei.value, PermanentError)  # base consumer dead-letters it
    assert c.metrics["invalid_payload"] == 1
