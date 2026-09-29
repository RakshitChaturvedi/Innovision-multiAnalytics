import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol
from uuid import UUID

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from shared.alerting.publisher import AlertPublisher, build_platform_alert
from shared.errors import PermanentError
from shared.platform_contracts.enums import AlertSeverity
from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.events import DetectionEvent

from ...policies.headcount_policy import (
    BreachState,
    Phase,
    Transition,
    step,
)
from ...policies.zone_policy import (
    get_centroid,
    point_in_polygon,
)
from ..zone_monitor.zone_store import ZoneStore
from .config import config as default_config
from .headcount_store import BreachRepository, HeadcountStore
from .window import RollingWindow

logger = logging.getLogger(__name__)


class InvalidPayload(PermanentError):
    """The message can never be processed; the base consumer dead-letters it."""


class AlertSink(Protocol):
    async def publish(self, alert) -> None: ...


@dataclass
class ZoneState:
    zone_id: str
    camera_id: str
    window: RollingWindow
    breach: BreachState = field(default_factory=BreachState)
    breach_id: str | None = None
    alert_published: bool = False
    zone_name: str = ""
    threshold: int | None = None
    last_snapshot_ts: datetime | None = None
    last_event_ts: datetime | None = None
    last_count: int = 0
    last_rolling_avg: float = 0.0


class HeadcountConsumer(BaseStreamConsumer):

    def __init__(
        self,
        *,
        cfg=default_config,
        zone_store: ZoneStore | None = None,
        repo: BreachRepository | None = None,
        cache: aioredis.Redis | None = None,
        alert_publisher: AlertSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(
            streams=[cfg.DETECTIONS_STREAM],
            group_name=cfg.CONSUMER_GROUP,
            consumer_name=cfg.CONSUMER_NAME,
        )
        self._cfg = cfg
        self._zone_store = zone_store
        self._repo = repo
        self._cache = cache
        self._alerts = alert_publisher
        self._clock = clock  # wall clock: ONLY for the stale-camera sweeper

        self._zones: dict[str, ZoneState] = {}
        self._camera_zones: dict[str, set[str]] = {}
        self._camera_last_seen: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: list[asyncio.Task] = []
        self.metrics: Counter[str] = Counter()

    # -- lifecycle -----------------------------------------------------

    async def start(self):
        redis_client = await aioredis.from_url(
            f"redis://{self._cfg.REDIS_HOST}:{self._cfg.REDIS_PORT}"
        )
        self._cache = redis_client
        self._alerts = AlertPublisher(
            redis_client,
            stream=self._cfg.ALERTS_STREAM,
            maxlen=self._cfg.ALERTS_MAXLEN,
        )

        self._zone_store = ZoneStore()
        await self._zone_store.initialize(redis_client)
        self._repo = HeadcountStore()

        await self.load_state()
        self._background.append(asyncio.create_task(self._run_sweeper()))
        logger.info("headcount_consumer_ready")
        await super().start()

    async def stop(self):
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()
        await super().stop()
        if self._cache is not None:
            await self._cache.aclose()
        if isinstance(self._repo, HeadcountStore):
            await self._repo.dispose()

    async def load_state(self) -> None:
        """Load open breaches once at startup; no DB query per frame."""
        for breach in await self._repo.load_open_breaches():
            zs = self._zone_state(breach.zone_id, breach.camera_id)
            zs.breach = BreachState(Phase.BREACH)
            zs.breach_id = breach.id
            zs.alert_published = breach.alert_published
            # Cover cameras that never send another event after a restart.
            self._camera_last_seen[breach.camera_id] = self._clock()
        logger.info("headcount_state_loaded open_breaches=%d", len(self._zones))

    # -- helpers -------------------------------------------------------

    def _zone_state(self, zone_id: str, camera_id: str) -> ZoneState:
        zs = self._zones.get(zone_id)
        if zs is None:
            zs = ZoneState(
                zone_id=zone_id,
                camera_id=camera_id,
                window=RollingWindow(self._cfg.ROLLING_WINDOW_SECONDS),
            )
            self._zones[zone_id] = zs
            self._camera_zones.setdefault(camera_id, set()).add(zone_id)
        return zs

    def _lock(self, camera_id: str) -> asyncio.Lock:
        return self._locks.setdefault(camera_id, asyncio.Lock())

    def _parse(self, msg_id: str, data: dict) -> DetectionEvent:
        raw = data.get(b"data") or data.get("data")
        if raw is None:
            self.metrics["invalid_payload"] += 1
            logger.warning("headcount_missing_data msg_id=%s", msg_id)
            raise InvalidPayload(f"msg {msg_id}: missing 'data' field")
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return DetectionEvent.model_validate_json(raw)
        except ValueError as exc:
            self.metrics["invalid_payload"] += 1
            logger.warning("headcount_bad_payload msg_id=%s error=%s", msg_id, exc)
            raise InvalidPayload(f"msg {msg_id}: {exc}") from exc

    # -- message processing -------------------------------------------

    async def process(self, msg_id: str, data: dict, stream: str) -> None:
        event = self._parse(msg_id, data)
        camera_id = str(event.camera_id)
        ts = event.timestamp

        async with self._lock(camera_id):
            self._camera_last_seen[camera_id] = self._clock()

            zones = await self._zone_store.get_zones(camera_id)
            centroids = [
                get_centroid(t.bbox)
                for t in event.tracks
                if t.class_label == "person"
            ]

            for zone in zones or []:
                max_headcount = zone.get("max_headcount")
                if max_headcount is None:
                    continue
                count = sum(
                    1 for c in centroids if point_in_polygon(c, zone["polygon"])
                )
                await self._process_zone(
                    event, camera_id, ts, zone, int(max_headcount), count
                )
            self.metrics["processed"] += 1

    async def _process_zone(
        self,
        event: DetectionEvent,
        camera_id: str,
        ts: datetime,
        zone: dict,
        max_headcount: int,
        count: int,
    ) -> None:
        zone_id = str(zone["id"])
        zs = self._zone_state(zone_id, camera_id)
        zs.zone_name = zone["name"]
        zs.threshold = max_headcount

        rolling_avg = zs.window.add(ts.timestamp(), count)
        zs.last_event_ts = ts
        zs.last_count = count
        zs.last_rolling_avg = rolling_avg

        await self._write_current(zs, count, rolling_avg, ts)

        interval = self._cfg.SNAPSHOT_INTERVAL_SECONDS
        if (
            zs.last_snapshot_ts is None
            or (ts - zs.last_snapshot_ts).total_seconds() >= interval
        ):
            await self._repo.write_snapshot(
                camera_id=camera_id,
                zone_id=zone_id,
                count=count,
                rolling_avg=rolling_avg,
                timestamp=ts,
            )
            zs.last_snapshot_ts = ts

        # Breach loaded at startup whose alert never made it out.
        if zs.breach_id and not zs.alert_published:
            await self._publish(zs, event, ts, count, rolling_avg)

        new_state, transition = step(
            zs.breach,
            rolling_avg,
            max_headcount,
            ts,
            self._cfg.BREACH_ENTER_SECONDS,
            self._cfg.BREACH_EXIT_SECONDS,
        )

        if transition is Transition.OPEN:
            opened = await self._repo.open_breach(
                zone_id, camera_id, count, max_headcount, ts
            )
            zs.breach_id = opened.id
            zs.alert_published = opened.alert_published
            if not opened.alert_published:
                await self._publish(zs, event, ts, count, rolling_avg)
            logger.warning(
                "headcount_breach camera=%s zone=%s count=%s avg=%.2f threshold=%s",
                camera_id, zone_id, count, rolling_avg, max_headcount,
            )
        elif transition is Transition.RESOLVE:
            await self._repo.resolve_breach(zs.breach_id, ts, "below_threshold")
            zs.breach_id = None
            zs.alert_published = False
            logger.info(
                "headcount_breach_resolved camera=%s zone=%s", camera_id, zone_id
            )

        # State is committed only after side effects succeeded, so a retry of
        # the same message re-derives the same transition.
        zs.breach = new_state

    async def _publish(
        self,
        zs: ZoneState,
        event: DetectionEvent,
        ts: datetime,
        count: int,
        rolling_avg: float,
    ) -> None:
        alert = build_platform_alert(
            domain_event_id=UUID(zs.breach_id),
            camera_id=UUID(zs.camera_id),
            timestamp=ts,
            severity=AlertSeverity.HIGH,
            alert_type="headcount_breach",
            title="Headcount Threshold Exceeded",
            description=(
                f"Zone '{zs.zone_name}' has an average of {rolling_avg:.1f} "
                f"persons (current {count}, threshold {zs.threshold})."
            ),
            frame_seq=event.frame_seq,
            metadata={
                "count": count,
                "rolling_avg": round(rolling_avg, 2),
                "threshold": zs.threshold,
                "zone_id": zs.zone_id,
                "zone_name": zs.zone_name,
            },
        )
        await self._alerts.publish(alert)
        await self._repo.mark_alert_published(zs.breach_id, str(alert.alert_id))
        zs.alert_published = True
        self.metrics["alerts_published"] += 1

    async def _write_current(
        self, zs: ZoneState, count: int, rolling_avg: float, ts: datetime
    ) -> None:
        # Dashboard convenience only: failure must not block alerting.
        try:
            await self._cache.set(
                f"{self._cfg.CURRENT_KEY_PREFIX}:{zs.zone_id}",
                json.dumps(
                    {
                        "count": count,
                        "rolling_avg": round(rolling_avg, 2),
                        "ts": ts.isoformat(),
                    }
                ),
                ex=self._cfg.CURRENT_TTL_SECONDS,
            )
        except RedisError as exc:
            self.metrics["current_cache_errors"] += 1
            logger.warning(
                "headcount_current_write_failed zone=%s error=%s", zs.zone_id, exc
            )

    # -- stale camera sweeper -----------------------------------------

    async def _run_sweeper(self) -> None:
        # BaseStreamConsumer._supervise only runs while `_running`, which
        # start() sets once the loop begins.
        while not self._running:
            await asyncio.sleep(0.05)
        await self._supervise("headcount_stale_sweeper", self._sweeper_loop)

    async def _sweeper_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._cfg.SWEEP_INTERVAL_S)
            await self.sweep_stale()

    async def sweep_stale(self) -> list[str]:
        """Resolve breaches of cameras silent for STALE_CAMERA_S (wall clock)."""
        now = self._clock()
        stale = [
            cam
            for cam, seen in self._camera_last_seen.items()
            if now - seen > self._cfg.STALE_CAMERA_S
        ]
        for camera_id in stale:
            async with self._lock(camera_id):
                # Re-check: an event may have arrived while waiting for the lock.
                if self._clock() - self._camera_last_seen.get(camera_id, 0) <= (
                    self._cfg.STALE_CAMERA_S
                ):
                    continue
                for zone_id in list(self._camera_zones.get(camera_id, ())):
                    zs = self._zones[zone_id]
                    if zs.breach_id:
                        # Wall clock only for breaches loaded at startup that
                        # have not seen an event since.
                        ts = zs.last_event_ts or datetime.now(timezone.utc)
                        await self._repo.resolve_breach(
                            zs.breach_id, ts, "camera_stale"
                        )
                        logger.warning(
                            "headcount_breach_resolved_stale camera=%s zone=%s",
                            camera_id, zone_id,
                        )
                        self.metrics["resolved_stale"] += 1
                    await self._drop_current(zone_id)
                    del self._zones[zone_id]
                self._camera_zones.pop(camera_id, None)
                self._camera_last_seen.pop(camera_id, None)
        return stale

    async def _drop_current(self, zone_id: str) -> None:
        try:
            await self._cache.delete(f"{self._cfg.CURRENT_KEY_PREFIX}:{zone_id}")
        except RedisError as exc:
            self.metrics["current_cache_errors"] += 1
            logger.warning("headcount_current_delete_failed zone=%s error=%s", zone_id, exc)
