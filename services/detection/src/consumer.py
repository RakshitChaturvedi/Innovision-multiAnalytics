"""
DetectionConsumer.

    Redis Stream -> FrameEvent -> fetch frame -> decode -> BatchManager
    -> YOLOv11m -> per-camera ByteTrack -> DetectionFilter
    -> DetectionPublisher -> PostgreSQL

The consumer does not implement YOLO, tracking or filtering logic.
"""

import asyncio
import json
import logging
import uuid

import cv2
import numpy as np
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.enums import FrameProvider
from shared.schemas.events import FrameEvent

from .batching import BatchManager, FrameItem
from .config import settings
from .detector import YOLODetector
from .face_estimator import FaceEstimator
from .filtering import DetectionFilter
from .model_loader import ModelLoader
from .publisher import DetectionPublisher
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


class DetectionConsumer(BaseStreamConsumer):
    """
    Consumes frames for the detection service.
    """

    def __init__(self) -> None:

        stream_key = f"frames:{settings.test_camera_id}"

        super().__init__(
            stream_key=stream_key,
            group_name=settings.consumer_group,
            consumer_name=settings.consumer_name,
        )

        self._model_loader = ModelLoader()
        self._detector: YOLODetector | None = None

        self._tracker_manager = TrackerManager()

        self._face_estimator = FaceEstimator(
            face_region_ratio=settings.face_region_ratio,
            face_area_min=settings.face_area_min,
        )

        self._filter = DetectionFilter(
            face_estimator=self._face_estimator,
            # FIX: was detection_confidence, i.e. the same threshold
            # YOLO already applied, so this filter was a no-op.
            min_confidence=settings.publish_confidence,
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

        self._side_redis: aioredis.Redis | None = None
        self._publisher: DetectionPublisher | None = None

        self._batch_processor_task: asyncio.Task | None = None
        self._stopping = False

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

        # 2. One shared connection for publishing + pubsub.
        self._side_redis = aioredis.from_url(
            settings.redis_url,
            health_check_interval=30,
        )

        self._publisher = DetectionPublisher(self._side_redis)

        # 3/4/5. Background workers.
        await self._batch_manager.start()

        self._batch_processor_task = asyncio.create_task(
            self._batch_processor_loop(),
            name="detection-batch-processor",
        )

        logger.info(
            "detection_consumer_ready model=yolov11m stream=%s "
            "batch_size=%d timeout_ms=%d",
            self.stream_key,
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

        # Drain what is already buffered before tearing down.
        await self._batch_manager.stop()

        if self._batch_processor_task is not None:
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
                pass

        await self._engine.dispose()

        logger.info("detection_consumer_stopped")

    # =========================================================
    # ACKNOWLEDGEMENT & STREAM PROCESSING
    # =========================================================

    async def _process_with_ack(self, msg_id: str, data: dict) -> None:
        """
        Override BaseStreamConsumer._process_with_ack.

        Do NOT auto-ack here: frames are enqueued to the BatchManager
        and must only be acked after batch inference, tracking,
        publishing, and database persistence have completed.
        """
        try:
            await self.process(msg_id, data)
        except Exception as e:
            logger.error(f"{self.consumer_name} failed on {msg_id}: {e}")

    async def ack(self, msg_id: str | bytes) -> None:
        """
        Explicitly acknowledge a processed message in Redis.
        """
        if self.redis is not None:
            await self.redis.xack(self.stream_key, self.group_name, msg_id)
        elif self._side_redis is not None:
            await self._side_redis.xack(self.stream_key, self.group_name, msg_id)

    # =========================================================
    # PROCESS REDIS MESSAGE
    # =========================================================

    async def process(self, msg_id, data: dict) -> None:

        raw = data.get(b"data") or data.get("data")

        if raw is None:
            logger.error(
                "frame_event_missing_data msg_id=%s", msg_id
            )
            # Malformed and unrecoverable: ack so it does not loop.
            await self.ack(msg_id)
            return

        if isinstance(raw, bytes):
            raw = raw.decode()

        try:
            frame_event = FrameEvent.model_validate_json(raw)
        except Exception as exc:
            logger.error(
                "frame_event_invalid msg_id=%s error=%s", msg_id, exc
            )
            await self.ack(msg_id)
            return

        # ---------------- fetch ----------------

        try:
            frame_bytes = await self._fetch_frame(frame_event)

        except Exception as exc:
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
            logger.error(
                "corrupt_frame camera_id=%s reference=%s",
                frame_event.camera_id,
                frame_event.frame_reference,
            )
            # Corrupt frames are permanent: ack and move on.
            await self.ack(msg_id)
            return

        await self._batch_manager.add(
            FrameItem(
                frame_event=frame_event,
                frame=frame,
                msg_id=msg_id,
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
                await self._process_batch(batch)

            except asyncio.CancelledError:
                break

            except Exception:
                logger.exception("batch_processor_error")
                await asyncio.sleep(0.01)

    # =========================================================
    # PROCESS BATCH
    # =========================================================

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

        for index, item in enumerate(batch.items):

            frame = item.frame
            frame_event = item.frame_event
            raw_detections = detections_per_frame[index]

            height, width = frame.shape[:2]

            tracker = self._tracker_manager.get(
                str(frame_event.camera_id)
            )

            tracked = tracker.update(raw_detections, frame)

            filtered = self._filter.apply(tracked, width, height)

            try:
                await self._publisher.publish(
                    frame_event=frame_event,
                    tracks=filtered,
                    inference_latency_ms=latency_ms,
                )

            except Exception:
                logger.exception(
                    "publish_failed camera_id=%s seq=%d",
                    frame_event.camera_id,
                    frame_event.frame_seq,
                )
                # Leave unacked -> retried later.
                continue

            db_rows.extend(
                self._build_rows(frame_event, filtered, latency_ms)
            )

            if item.msg_id is not None:
                acked.append(item.msg_id)

            logger.info(
                "frame_processed model=yolov11m camera_id=%s seq=%d "
                "tracks=%d latency_ms=%.1f",
                frame_event.camera_id,
                frame_event.frame_seq,
                len(filtered),
                latency_ms,
            )

        await self._write_to_db(db_rows)

        # Only now is the frame durably handled.
        for msg_id in acked:
            try:
                await self.ack(msg_id)
            except Exception:
                logger.warning("ack_failed msg_id=%s", msg_id)

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
            logger.exception(
                "detection_db_write_failed rows=%d", len(rows)
            )

    # =========================================================
    # FRAME FETCH
    # =========================================================

    async def _fetch_frame(self, frame_event: FrameEvent) -> bytes:
        """
        Fetch the live frame from Redis cache.

        MinIO is cold storage only and is not used by the
        real-time detection pipeline.
        """

        if frame_event.frame_provider != FrameProvider.REDIS:
            raise RuntimeError(
                f"Unsupported frame provider for detection: "
                f"{frame_event.frame_provider}"
            )

        if self._side_redis is None:
            raise RuntimeError(
                "Redis connection is not initialized."
            )

        ref = frame_event.frame_reference
        key = ref if ref.startswith("frames:") else f"frames:{ref}"

        try:
            data = await self._side_redis.get(key)

        except Exception as exc:
            logger.error(
                "redis_frame_fetch_failed reference=%s key=%s error=%s",
                frame_event.frame_reference,
                key,
                exc,
            )
            raise

        if data is None:
            raise RuntimeError(
                f"Frame not found in Redis cache: "
                f"{key}"
            )

        return data
