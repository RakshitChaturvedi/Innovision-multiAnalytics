"""
DetectionConsumer.

    frames:{camera_id} (one stream per camera) -> FrameEvent -> fetch frame
    -> decode -> BatchManager -> YOLOv11m -> per-camera ByteTrack
    -> global track ids -> DetectionFilter -> DetectionPublisher -> PostgreSQL

The consumer does not implement YOLO, tracking or filtering logic.
"""

import asyncio
import dataclasses
import json
import logging
import time
import uuid

import cv2
import numpy as np
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from shared.errors import PermanentError
from shared.frames import FrameUnavailable, fetch_frame
from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.events import FrameEvent
from shared.storage.storage_minio_client import StorageClient

from .batching import BatchManager, FrameItem
from .camera_discovery import (
    RegistryError,
    RegistryShapeError,
    fetch_registry_camera_ids,
    parse_static_camera_ids,
)
from .config import settings
from .detector import YOLODetector
from .face_estimator import FaceEstimator
from .filtering import DetectionFilter
from .model_loader import ModelLoader
from .publisher import DetectionPublisher
from .track_ids import TrackIdAllocator
from .tracker import TrackerManager

logger = logging.getLogger(__name__)


_INSERT_DETECTION_SQL = text(
    """
    INSERT INTO detection_events (
        id,
        camera_id,
        track_id,
        bounding_box,
        class_label,
        confidence,
        has_face,
        face_bbox,
        frame_reference,
        frame_provider,
        frame_seq,
        frame_timestamp,
        inference_latency_ms
    )
    VALUES (
        CAST(:id AS uuid),
        CAST(:camera_id AS uuid),
        :track_id,
        CAST(:bounding_box AS jsonb),
        :class_label,
        :confidence,
        :has_face,
        CAST(:face_bbox AS jsonb),
        :frame_reference,
        :frame_provider,
        :frame_seq,
        :frame_timestamp,
        :inference_latency_ms
    )
    ON CONFLICT DO NOTHING
    """
)


@dataclasses.dataclass
class CameraProgress:
    """What has been fed to a camera's tracker so far (in memory)."""

    last_seq: int
    last_ts: object
    # Frames the tracker consumed but whose publish failed: seq -> (tracks,
    # first_failure_monotonic). While non-empty, later frames of the camera
    # wait, so detections are never published out of order.
    pending: dict = dataclasses.field(default_factory=dict)


class DetectionConsumer(BaseStreamConsumer):
    """
    Consumes frames for the detection service, one stream per camera.
    """

    # Frames are acked only after batch inference, tracking, publishing and
    # DB persistence (see _process_batch), never right after process().
    auto_ack = False

    def __init__(
        self,
        *,
        detector=None,
        tracker_manager: TrackerManager | None = None,
        side_redis: aioredis.Redis | None = None,
        storage=None,
        registry_get_json=None,
    ) -> None:

        super().__init__(
            streams=[],
            group_name=settings.consumer_group,
            consumer_name=settings.consumer_name,
        )

        self._static_camera_ids = parse_static_camera_ids(
            settings.detection_camera_ids
        )
        self._registry_get_json = registry_get_json

        self._model_loader = ModelLoader()
        self._detector: YOLODetector | None = detector

        self._tracker_manager = tracker_manager or TrackerManager()
        self._track_ids: TrackIdAllocator | None = None

        self._face_estimator = FaceEstimator(
            face_region_ratio=settings.face_region_ratio,
            face_area_min=settings.face_area_min,
        )

        # No confidence filter after tracking: ByteTrack rows are already
        # activated tracks; re-filtering made tracks flicker.
        self._filter = DetectionFilter(
            face_estimator=self._face_estimator,
        )

        self._batch_manager = BatchManager(
            batch_size=settings.batch_size,
            batch_timeout_ms=settings.batch_timeout_ms,
            max_pending_batches=settings.max_pending_batches,
        )

        self._engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

        self._side_redis: aioredis.Redis | None = side_redis
        self._storage = storage if storage is not None else StorageClient()
        self._publisher: DetectionPublisher | None = None

        self._progress: dict[str, CameraProgress] = {}
        # A pending (unpublished) frame is forgotten after the base consumer
        # has had time to deliver it max_deliveries times and dead-letter it.
        self._pending_max_age_s = (
            self.max_deliveries * self.reclaim_idle_ms / 1000 + 60
        )

        self._batch_processor_task: asyncio.Task | None = None
        self._extra_tasks: list[asyncio.Task] = []
        self._stopping = False

    def _count(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    # =========================================================
    # CAMERA DISCOVERY
    # =========================================================

    @staticmethod
    def _stream_for(camera_id: str) -> str:
        return f"frames:{camera_id}"

    async def _discover_cameras(self) -> list[str] | None:
        """Desired camera list, or None to keep the current one."""
        if self._static_camera_ids:
            return list(self._static_camera_ids)

        if not settings.camera_registry_url:
            return None

        kwargs = {}
        if self._registry_get_json is not None:
            kwargs["get_json"] = self._registry_get_json
        try:
            return await fetch_registry_camera_ids(
                settings.camera_registry_url,
                settings.source_uc,
                settings.camera_registry_timeout_s,
                **kwargs,
            )
        except RegistryShapeError as exc:
            self._count("registry_errors")
            logger.error(
                "camera_registry_bad_response keeping %d camera(s): %s",
                len(self.streams), exc,
            )
            return None
        except RegistryError as exc:
            self._count("registry_errors")
            logger.warning(
                "camera_registry_unavailable keeping %d camera(s): %s",
                len(self.streams), exc,
            )
            return None

    async def _sync_cameras(self) -> None:
        cameras = await self._discover_cameras()
        if cameras is None:
            return

        desired = {self._stream_for(c): c for c in cameras}
        for stream in list(self.streams):
            if stream not in desired:
                await self.remove_stream(stream)
                camera_id = stream.removeprefix("frames:")
                self._tracker_manager.drop(camera_id)
                self._progress.pop(camera_id, None)
                if self._track_ids is not None:
                    self._track_ids.drop_camera(camera_id)
                logger.info("camera_removed camera_id=%s", camera_id)
        for stream, camera_id in desired.items():
            if stream not in self.streams:
                await self.add_stream(stream)
                logger.info("camera_added camera_id=%s", camera_id)

    async def _run_camera_discovery(self) -> None:
        # BaseStreamConsumer._supervise only runs while `_running`, which
        # start() sets once the read loop begins.
        while not self._running:
            await asyncio.sleep(0.05)
        await self._supervise("camera_discovery", self._camera_discovery_loop)

    async def _camera_discovery_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.camera_refresh_s)
            await self._sync_cameras()

    # =========================================================
    # START
    # =========================================================

    async def start(self) -> None:

        # 1. Load YOLOv11m off the event loop; it can take seconds.
        await asyncio.to_thread(self._model_loader.preload)

        # Build the detector once, not once per batch.
        self._detector = YOLODetector(
            self._model_loader.get_model()
        )

        # 2. One shared connection for publishing + frame fetch.
        self._side_redis = aioredis.from_url(
            settings.redis_url,
            health_check_interval=30,
        )

        self._publisher = DetectionPublisher(self._side_redis)
        self._track_ids = TrackIdAllocator(
            self._side_redis, track_buffer=settings.track_buffer
        )

        # 3. Cameras: initial list now, refreshed in the background.
        if not self._static_camera_ids and not settings.camera_registry_url:
            logger.warning(
                "no cameras configured: set DETECTION_CAMERA_IDS or "
                "CAMERA_REGISTRY_URL"
            )
        await self._sync_cameras()
        if not self._static_camera_ids and settings.camera_registry_url:
            self._extra_tasks.append(
                asyncio.create_task(
                    self._run_camera_discovery(), name="detection-cameras"
                )
            )

        # 4. Background workers.
        await self._batch_manager.start()

        self._batch_processor_task = asyncio.create_task(
            self._batch_processor_loop(),
            name="detection-batch-processor",
        )

        logger.info(
            "detection_consumer_ready model=yolov11m streams=%s "
            "batch_size=%d timeout_ms=%d",
            self.streams,
            settings.batch_size,
            settings.batch_timeout_ms,
        )

        await super().start()

    # =========================================================
    # STOP
    # =========================================================

    async def stop(self) -> None:

        if self._stopping:
            return

        self._stopping = True

        for task in self._extra_tasks:
            task.cancel()
        await asyncio.gather(*self._extra_tasks, return_exceptions=True)
        self._extra_tasks.clear()

        # 1. Stop reading and let in-flight process() calls finish, so no
        #    frame can be added to the batch manager after it is flushed.
        await self._quiesce_readers()

        # 2. Flush the frames still buffered, then let the processor work
        #    off every ready batch BEFORE cancelling it.
        await self._batch_manager.stop()

        if self._batch_processor_task is not None:
            try:
                await asyncio.wait_for(
                    self._batch_manager.join(), self.stop_timeout_s
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "detection_stop_drain_timeout pending_batches=%d; "
                    "their frames stay pending and are reclaimed on restart",
                    self._batch_manager.pending_batches(),
                )

            self._batch_processor_task.cancel()
            try:
                await self._batch_processor_task
            except (asyncio.CancelledError, Exception):
                pass

        await super().stop()

        if self._side_redis is not None:
            try:
                await self._side_redis.aclose()
            except Exception:
                logger.warning("side_redis_close_failed", exc_info=True)

        await self._engine.dispose()

        logger.info("detection_consumer_stopped")

    async def _quiesce_readers(self) -> None:
        """Stop the read loop and wait for in-flight process() calls."""
        self._running = False
        if self._started:
            try:
                await asyncio.wait_for(
                    self._loop_done.wait(), self.stop_timeout_s
                )
            except asyncio.TimeoutError:
                logger.warning("detection main loop did not exit in time")
        inflight = [t for t in self._inflight if not t.done()]
        if inflight:
            await asyncio.wait(inflight, timeout=self.stop_timeout_s)

    # =========================================================
    # PROCESS REDIS MESSAGE
    # =========================================================

    async def process(self, msg_id, data: dict, stream: str) -> None:

        raw = data.get(b"data") or data.get("data")

        if raw is None:
            self._count("invalid_payload")
            logger.warning("frame_event_missing_data msg_id=%s", msg_id)
            raise PermanentError(f"msg {msg_id}: missing 'data' field")

        if isinstance(raw, bytes):
            raw = raw.decode()

        try:
            frame_event = FrameEvent.model_validate_json(raw)
        except ValueError as exc:
            self._count("invalid_payload")
            logger.warning(
                "frame_event_invalid msg_id=%s error=%s", msg_id, exc
            )
            raise PermanentError(f"msg {msg_id}: invalid FrameEvent: {exc}") from exc

        # ---------------- fetch ----------------

        try:
            frame_bytes = await self._fetch_frame(frame_event)

        except FrameUnavailable as exc:
            # Gone from Redis and MinIO: retrying can never help, and it is
            # routine when detection lags behind the 20 s Redis TTL.
            self._count("frame_expired")
            logger.warning(
                "frame_expired camera_id=%s seq=%d reference=%s: %s",
                frame_event.camera_id,
                frame_event.frame_seq,
                frame_event.frame_reference,
                exc,
            )
            await self.ack(stream, msg_id)
            return

        except Exception as exc:
            self._count("frame_fetch_errors")
            logger.error(
                "frame_fetch_failed camera_id=%s provider=%s "
                "reference=%s error=%s",
                frame_event.camera_id,
                frame_event.frame_provider,
                frame_event.frame_reference,
                exc,
            )
            # Not acked -> reclaimed and retried by the base consumer.
            raise

        # ---------------- decode ----------------

        frame = await asyncio.to_thread(self._decode, frame_bytes)

        if frame is None:
            self._count("corrupt_frame")
            logger.warning(
                "corrupt_frame camera_id=%s reference=%s",
                frame_event.camera_id,
                frame_event.frame_reference,
            )
            raise PermanentError(
                f"corrupt frame camera={frame_event.camera_id} "
                f"seq={frame_event.frame_seq}"
            )

        await self._batch_manager.add(
            FrameItem(
                frame_event=frame_event,
                frame=frame,
                msg_id=msg_id,
                stream=stream,
            )
        )

    @staticmethod
    def _decode(frame_bytes: bytes):
        array = np.frombuffer(frame_bytes, dtype=np.uint8)
        return cv2.imdecode(array, cv2.IMREAD_COLOR)

    # =========================================================
    # BATCH PROCESSOR LOOP
    # =========================================================

    async def _batch_processor_loop(self) -> None:

        while True:
            try:
                batch = await self._batch_manager.get_ready_batch()
            except asyncio.CancelledError:
                break

            try:
                await self._process_batch(batch)

            except asyncio.CancelledError:
                break

            except Exception:
                # The batch's messages were not acked: the base consumer
                # reclaims them and dead-letters after max_deliveries.
                self._count("batch_errors")
                logger.exception("batch_processor_error")
                await asyncio.sleep(0.01)

            finally:
                self._batch_manager.task_done()

    # =========================================================
    # PROCESS BATCH
    # =========================================================

    def _forget_expired_pending(self, camera_id: str, progress: CameraProgress) -> None:
        now = time.monotonic()
        for seq, (_, since) in list(progress.pending.items()):
            if now - since > self._pending_max_age_s:
                del progress.pending[seq]
                self._count("pending_expired")
                logger.warning(
                    "pending_frame_expired camera_id=%s seq=%d "
                    "(base consumer dead-letters it after max_deliveries)",
                    camera_id, seq,
                )

    async def _process_batch(self, batch) -> None:

        if not batch.items:
            return

        if self._publisher is None or self._detector is None:
            raise RuntimeError(
                "DetectionConsumer.start() was not called."
            )

        # YOLO inference is CPU/GPU-bound and releases the GIL inside
        # torch, so a worker thread keeps the event loop responsive.
        (
            detections_per_frame,
            latency_ms,
        ) = await asyncio.to_thread(
            self._detector.run_batch, batch.frames
        )

        db_rows: list[dict] = []
        acked: list = []

        # Items are handled strictly in batch order, so each camera's
        # frames reach its tracker in sequence.
        for index, item in enumerate(batch.items):

            frame = item.frame
            frame_event = item.frame_event
            camera_id = str(frame_event.camera_id)
            seq = frame_event.frame_seq
            progress = self._progress.get(camera_id)

            if progress is not None:
                self._forget_expired_pending(camera_id, progress)

            ack_key = (
                (item.stream, item.msg_id)
                if item.msg_id is not None and item.stream is not None
                else None
            )

            filtered = None
            retry = False

            if progress is not None and seq in progress.pending:
                # Redelivery of a frame the tracker already consumed whose
                # publish failed: republish the same tracks. Only the oldest
                # pending frame may go first, to keep publish order.
                if seq != min(progress.pending):
                    self._count("deferred")
                    continue
                filtered = progress.pending[seq][0]
                retry = True

            elif progress is not None and progress.pending:
                # An earlier frame of this camera is unpublished: wait for
                # it (left unacked, reclaimed later) instead of overtaking.
                self._count("deferred")
                continue

            elif progress is not None and (
                seq <= progress.last_seq
                and frame_event.timestamp <= progress.last_ts
            ):
                # Already processed (reclaim of a message still queued, or
                # ack failed earlier). Never feed old frames to ByteTrack.
                self._count("duplicate_frame")
                logger.warning(
                    "duplicate_or_stale_frame camera_id=%s seq=%d last_seq=%d; acking",
                    camera_id, seq, progress.last_seq,
                )
                if ack_key:
                    acked.append(ack_key)
                continue

            if filtered is None:
                if (
                    not retry
                    and progress is not None
                    and seq <= progress.last_seq
                ):
                    # Lower seq but newer timestamp: the source restarted
                    # its sequence. Local track ids restart with the tracker.
                    logger.warning(
                        "frame_seq_reset camera_id=%s seq=%d last_seq=%d; "
                        "resetting tracker",
                        camera_id, seq, progress.last_seq,
                    )
                    self._tracker_manager.reset(camera_id)
                    self._track_ids.drop_camera(camera_id)
                    progress = None

                if progress is None:
                    progress = CameraProgress(seq, frame_event.timestamp)
                    self._progress[camera_id] = progress
                elif not retry:
                    progress.last_seq = seq
                    progress.last_ts = frame_event.timestamp

                height, width = frame.shape[:2]
                tracker = self._tracker_manager.get(camera_id)
                resets_before = tracker.reset_count

                tracked = tracker.update(detections_per_frame[index], frame)

                if tracker.reset_count != resets_before:
                    # Tracker crashed and restarted: old mappings are void.
                    self._track_ids.drop_camera(camera_id)

                try:
                    global_ids = await self._track_ids.assign(
                        camera_id, [t.track_id for t in tracked]
                    )
                except Exception:
                    # The tracker consumed this frame but we have no tracks
                    # to publish: hold the camera at this frame; the retry
                    # recomputes it (re-updating the tracker with the same
                    # detections is harmless).
                    self._count("track_id_alloc_failed")
                    logger.exception(
                        "track_id_alloc_failed camera_id=%s seq=%d",
                        camera_id, seq,
                    )
                    progress.pending.setdefault(seq, (None, time.monotonic()))
                    continue

                tracked = [
                    dataclasses.replace(t, track_id=global_ids[t.track_id])
                    for t in tracked
                ]
                filtered = self._filter.apply(tracked, width, height)

            try:
                await self._publisher.publish(
                    frame_event=frame_event,
                    tracks=filtered,
                    inference_latency_ms=latency_ms,
                )

            except Exception:
                self._count("publish_failed")
                logger.exception(
                    "publish_failed camera_id=%s seq=%d",
                    frame_event.camera_id,
                    frame_event.frame_seq,
                )
                since = progress.pending.get(seq, (None, time.monotonic()))[1]
                progress.pending[seq] = (filtered, since)
                # Leave unacked -> reclaimed, retried, dead-lettered after
                # max_deliveries.
                continue

            progress.pending.pop(seq, None)

            db_rows.extend(
                self._build_rows(frame_event, filtered, latency_ms)
            )

            if ack_key:
                acked.append(ack_key)

            logger.info(
                "frame_processed model=yolov11m camera_id=%s seq=%d "
                "tracks=%d latency_ms=%.1f%s",
                frame_event.camera_id,
                frame_event.frame_seq,
                len(filtered),
                latency_ms,
                " (retry)" if retry else "",
            )

        await self._write_to_db(db_rows)

        # Only now is the frame durably handled.
        for stream, msg_id in acked:
            try:
                await self.ack(stream, msg_id)
            except Exception:
                # Not acked -> stays pending and is redelivered by reclaim.
                self._count("ack_failed")
                logger.warning("ack_failed stream=%s msg_id=%s", stream, msg_id)

    # =========================================================
    # DATABASE
    # =========================================================

    @staticmethod
    def _build_rows(
        frame_event: FrameEvent,
        tracks: list,
        latency_ms: float,
    ) -> list[dict]:

        rows: list[dict] = []

        for track in tracks:
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "camera_id": str(frame_event.camera_id),
                    "track_id": track.track_id,
                    # asyncpg cannot adapt a dict to jsonb; encode it
                    # and cast in SQL.
                    "bounding_box": json.dumps(
                        track.bbox.model_dump()
                    ),
                    "class_label": track.class_label,
                    "confidence": track.confidence,
                    "has_face": track.has_face,
                    "face_bbox": (
                        json.dumps(track.face_bbox.model_dump())
                        if track.face_bbox
                        else None
                    ),
                    "frame_reference": frame_event.frame_reference,
                    "frame_provider": (
                        frame_event.frame_provider.value
                        if frame_event.frame_provider
                        else None
                    ),
                    "frame_seq": frame_event.frame_seq,
                    "frame_timestamp": frame_event.timestamp,
                    "inference_latency_ms": latency_ms,
                }
            )

        return rows

    async def _write_to_db(self, rows: list[dict]) -> None:
        """
        One executemany per batch instead of one round-trip per track.
        """

        if not rows:
            return

        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    _INSERT_DETECTION_SQL, rows
                )

        except Exception:
            # A detection already published to Redis must not be lost
            # because Postgres is briefly unavailable.
            self._count("db_write_failed")
            logger.exception(
                "detection_db_write_failed rows=%d", len(rows)
            )

    # =========================================================
    # FRAME FETCH
    # =========================================================

    async def _fetch_frame(self, frame_event: FrameEvent) -> bytes:
        """
        Fetch the frame: Redis key verbatim first, MinIO cold copy on a miss.

        Raises FrameUnavailable (a PermanentError) if the frame is gone;
        connection errors propagate so the message is retried.
        """

        if self._side_redis is None:
            raise RuntimeError(
                "Redis connection is not initialized."
            )

        return await fetch_frame(
            self._side_redis,
            self._storage,
            camera_id=frame_event.camera_id,
            frame_seq=frame_event.frame_seq,
            frame_reference=frame_event.frame_reference,
            frame_provider=frame_event.frame_provider,
        )
