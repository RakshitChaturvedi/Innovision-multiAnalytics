"""
DetectionConsumer.

Responsibilities:

    Redis Stream
        ↓
    FrameEvent
        ↓
    Fetch frame
        ↓
    Decode frame
        ↓
    BatchManager
        ↓
    YOLOv11m
        ↓
    Per-camera ByteTrack
        ↓
    DetectionFilter
        ↓
    DetectionPublisher
        ↓
    PostgreSQL

The consumer itself does not implement
YOLO, tracking or filtering logic.
"""

import asyncio
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

from shared.schemas.consumer import (
    BaseStreamConsumer,
)

from shared.schemas.contracts import (
    FrameEvent,
    FrameProvider,
)

from shared.storage.minio_client import (
    storage,
)

from .batching import (
    BatchManager,
    FrameItem,
)

from .config import settings

from .detector import (
    YOLODetector,
)

from .face_estimator import (
    FaceEstimator,
)

from .filtering import (
    DetectionFilter,
)

from .model_loader import (
    ModelLoader,
)

from .publisher import (
    DetectionPublisher,
)

from .tracker import (
    TrackerManager,
)


logger = logging.getLogger(__name__)


# =============================================================
# DETECTION CONSUMER
# =============================================================

class DetectionConsumer(
    BaseStreamConsumer
):
    """
    Consumes frames for the detection service.

    Sprint 2 uses a hardcoded test camera.

    CameraProfile is NOT used.
    """

    def __init__(self) -> None:

        # -----------------------------------------------------
        # Current Sprint 2 camera
        # -----------------------------------------------------

        stream_key = (
            f"frames:{settings.test_camera_id}"
        )

        super().__init__(
            stream_key=stream_key,

            group_name=(
                settings.consumer_group
            ),

            consumer_name=(
                settings.consumer_name
            ),
        )

        # -----------------------------------------------------
        # Model
        # -----------------------------------------------------

        self._model_loader = (
            ModelLoader()
        )

        # -----------------------------------------------------
        # Tracker
        # -----------------------------------------------------

        self._tracker_manager = (
            TrackerManager()
        )

        # -----------------------------------------------------
        # Face estimator
        # -----------------------------------------------------

        self._face_estimator = (
            FaceEstimator(
                face_region_ratio=(
                    settings.face_region_ratio
                ),
                face_area_min=(
                    settings.face_area_min
                ),
            )
        )

        # -----------------------------------------------------
        # Filtering
        # -----------------------------------------------------

        self._filter = (
            DetectionFilter(
                face_estimator=(
                    self._face_estimator
                ),
                min_confidence=(
                    settings.detection_confidence
                ),
            )
        )

        # -----------------------------------------------------
        # Batching
        # -----------------------------------------------------

        self._batch_manager = (
            BatchManager(
                batch_size=(
                    settings.batch_size
                ),
                batch_timeout_ms=(
                    settings.batch_timeout_ms
                ),
            )
        )

        # -----------------------------------------------------
        # PostgreSQL
        # -----------------------------------------------------

        self._engine = (
            create_async_engine(
                settings.database_url
            )
        )

        self._session_factory = (
            async_sessionmaker(
                self._engine,
                expire_on_commit=False,
            )
        )

        # -----------------------------------------------------
        # Runtime tasks
        # -----------------------------------------------------

        self._publisher: (
            DetectionPublisher | None
        ) = None

        self._batch_processor_task: (
            asyncio.Task | None
        ) = None

        self._shake_subscriber_task: (
            asyncio.Task | None
        ) = None

    # =========================================================
    # START
    # =========================================================

    async def start(self) -> None:
        """
        Start detection service.
        """

        # -----------------------------------------------------
        # 1. Load YOLOv11m
        # -----------------------------------------------------

        self._model_loader.preload()

        # -----------------------------------------------------
        # 2. Create Redis publisher connection
        # -----------------------------------------------------

        publisher_redis = (
            await aioredis.from_url(
                (
                    f"redis://"
                    f"{settings.redis_host}:"
                    f"{settings.redis_port}"
                )
            )
        )

        self._publisher = (
            DetectionPublisher(
                publisher_redis
            )
        )

        # -----------------------------------------------------
        # 3. Start batch manager
        # -----------------------------------------------------

        await self._batch_manager.start()

        # -----------------------------------------------------
        # 4. Start batch processor
        # -----------------------------------------------------

        self._batch_processor_task = (
            asyncio.create_task(
                self._batch_processor_loop()
            )
        )

        # -----------------------------------------------------
        # 5. Start camera shake subscriber
        # -----------------------------------------------------

        self._shake_subscriber_task = (
            asyncio.create_task(
                self._shake_subscriber()
            )
        )

        logger.info(
            "detection_consumer_ready "
            "model=yolov11m "
            "stream=%s "
            "batch_size=%d "
            "timeout_ms=%d",
            self.stream_key,
            settings.batch_size,
            settings.batch_timeout_ms,
        )

        # -----------------------------------------------------
        # 6. Start Redis stream consumer
        # -----------------------------------------------------

        await super().start()

    # =========================================================
    # STOP
    # =========================================================

    async def stop(self) -> None:
        """
        Gracefully stop the detection service.
        """

        await self._batch_manager.stop()

        if (
            self._batch_processor_task
            is not None
        ):

            self._batch_processor_task.cancel()

            try:
                await (
                    self._batch_processor_task
                )
            except asyncio.CancelledError:
                pass

        if (
            self._shake_subscriber_task
            is not None
        ):

            self._shake_subscriber_task.cancel()

            try:
                await (
                    self._shake_subscriber_task
                )
            except asyncio.CancelledError:
                pass

        await super().stop()

        await self._engine.dispose()

    # =========================================================
    # PROCESS REDIS MESSAGE
    # =========================================================

    async def process(
        self,
        msg_id: str,
        data: dict,
    ) -> None:
        """
        Process one Redis Stream message.

        The message contains a FrameEvent.
        """

        raw = (
            data.get(b"data")
            or data.get("data")
        )

        if raw is None:
            logger.error(
                "frame_event_missing_data "
                "msg_id=%s",
                msg_id,
            )
            return

        if isinstance(raw, bytes):
            raw = raw.decode()

        # -----------------------------------------------------
        # Parse FrameEvent
        # -----------------------------------------------------

        frame_event = (
            FrameEvent.model_validate_json(
                raw
            )
        )

        # -----------------------------------------------------
        # Fetch frame
        # -----------------------------------------------------

        try:

            frame_bytes = (
                await self._fetch_frame(
                    frame_event
                )
            )

        except Exception as exc:

            logger.error(
                "frame_fetch_failed "
                "camera_id=%s "
                "provider=%s "
                "reference=%s "
                "error=%s",
                frame_event.camera_id,
                frame_event.frame_provider,
                frame_event.frame_reference,
                exc,
            )

            # Do NOT acknowledge.
            #
            # BaseStreamConsumer should redeliver.
            raise

        # -----------------------------------------------------
        # Decode JPEG
        # -----------------------------------------------------

        frame_array = np.frombuffer(
            frame_bytes,
            dtype=np.uint8,
        )

        frame = cv2.imdecode(
            frame_array,
            cv2.IMREAD_COLOR,
        )

        if frame is None:

            logger.error(
                "corrupt_frame "
                "camera_id=%s "
                "reference=%s",
                frame_event.camera_id,
                frame_event.frame_reference,
            )

            # Corrupt frames are permanent.
            return

        # -----------------------------------------------------
        # Add to global batch
        # -----------------------------------------------------

        await self._batch_manager.add(
            FrameItem(
                frame_event=frame_event,
                frame=frame,
            )
        )

        # BaseStreamConsumer acknowledges after
        # process() returns normally.

    # =========================================================
    # BATCH PROCESSOR LOOP
    # =========================================================

    async def _batch_processor_loop(
        self,
    ) -> None:
        """
        Continuously process ready batches.
        """

        while True:

            try:

                batch = (
                    await self._batch_manager
                    .get_ready_batch()
                )

                await self._process_batch(
                    batch
                )

            except asyncio.CancelledError:

                break

            except Exception:

                logger.exception(
                    "batch_processor_error"
                )

                # Do not kill the worker because
                # one batch failed.

                await asyncio.sleep(
                    0.01
                )

    # =========================================================
    # PROCESS BATCH
    # =========================================================

    async def _process_batch(
        self,
        batch,
    ) -> None:
        """
        Full detection pipeline:

            YOLOv11m
                ↓
            per-camera ByteTrack
                ↓
            filtering
                ↓
            publisher
                ↓
            PostgreSQL
        """

        if not batch.items:
            return

        if self._publisher is None:
            raise RuntimeError(
                "DetectionPublisher is not initialized."
            )

        # -----------------------------------------------------
        # ONE SHARED MODEL
        # -----------------------------------------------------

        model = (
            self._model_loader.get_model()
        )

        detector = YOLODetector(
            model
        )

        # -----------------------------------------------------
        # YOLOv11m BATCH INFERENCE
        # -----------------------------------------------------

        (
            detections_per_frame,
            latency_ms,
        ) = detector.run_batch(
            batch.frames
        )

        # -----------------------------------------------------
        # Process every frame separately
        # -----------------------------------------------------

        for index, item in enumerate(
            batch.items
        ):

            frame = item.frame

            frame_event = (
                item.frame_event
            )

            raw_detections = (
                detections_per_frame[
                    index
                ]
            )

            height, width = (
                frame.shape[:2]
            )

            # -------------------------------------------------
            # PER-CAMERA TRACKER
            # -------------------------------------------------

            tracker = (
                self._tracker_manager.get(
                    str(
                        frame_event.camera_id
                    )
                )
            )

            tracked = tracker.update(
                raw_detections,
                frame,
            )

            # -------------------------------------------------
            # FILTER
            # -------------------------------------------------

            filtered = (
                self._filter.apply(
                    tracked,
                    width,
                    height,
                )
            )

            # -------------------------------------------------
            # PUBLISH
            # -------------------------------------------------

            await self._publisher.publish(
                frame_event=frame_event,
                tracks=filtered,
                inference_latency_ms=(
                    latency_ms
                ),
            )

            # -------------------------------------------------
            # DATABASE
            # -------------------------------------------------

            await self._write_to_db(
                frame_event=frame_event,
                tracks=filtered,
                latency_ms=latency_ms,
            )

            logger.info(
                "frame_processed "
                "model=yolov11m "
                "camera_id=%s "
                "seq=%d "
                "tracks=%d "
                "latency_ms=%.1f",
                frame_event.camera_id,
                frame_event.frame_seq,
                len(filtered),
                latency_ms,
            )

    # =========================================================
    # DATABASE WRITE
    # =========================================================

    async def _write_to_db(
        self,
        frame_event: FrameEvent,
        tracks: list,
        latency_ms: float,
    ) -> None:
        """
        Write detection events to the UC1-internal
        PostgreSQL detection_events table.
        """

        if not tracks:
            return

        async with (
            self._session_factory() as session
        ):

            async with session.begin():

                for track in tracks:

                    await session.execute(
                        text(
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
                                frame_timestamp,
                                inference_latency_ms
                            )
                            VALUES (
                                :id,
                                :camera_id,
                                :track_id,
                                :bounding_box,
                                :class_label,
                                :confidence,
                                :has_face,
                                :face_bbox,
                                :frame_reference,
                                :frame_provider,
                                :frame_timestamp,
                                :inference_latency_ms
                            )
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {
                            "id": str(
                                uuid.uuid4()
                            ),

                            "camera_id": str(
                                frame_event.camera_id
                            ),

                            "track_id": (
                                track.track_id
                            ),

                            "bounding_box": (
                                track.bbox.model_dump()
                            ),

                            "class_label": (
                                track.class_label
                            ),

                            "confidence": (
                                track.confidence
                            ),

                            "has_face": (
                                track.has_face
                            ),

                            "face_bbox": (
                                track.face_bbox.model_dump()
                                if track.face_bbox
                                else None
                            ),

                            "frame_reference": (
                                frame_event.frame_reference
                            ),

                            "frame_provider": (
                                frame_event
                                .frame_provider.value
                                if frame_event.frame_provider
                                else None
                            ),

                            "frame_timestamp": (
                                frame_event.timestamp
                            ),

                            "inference_latency_ms": (
                                latency_ms
                            ),
                        },
                    )

    # =========================================================
    # FRAME FETCH
    # =========================================================

    async def _fetch_frame(
        self,
        frame_event: FrameEvent,
    ) -> bytes:
        """
        Fetch frame bytes.

        CACHE:
            Try Redis first.

        MINIO:
            Used as fallback.

        Other providers:
            Currently fall back to MinIO.
        """

        # -----------------------------------------------------
        # CACHE
        # -----------------------------------------------------

        if (
            frame_event.frame_provider
            == FrameProvider.CACHE
        ):

            try:

                data = await self.redis.get(
                    frame_event.frame_reference
                )

                if data is not None:
                    return data

                logger.warning(
                    "cache_miss_falling_back_to_minio "
                    "reference=%s",
                    frame_event.frame_reference,
                )

            except Exception as exc:

                logger.warning(
                    "cache_fetch_error "
                    "reference=%s "
                    "error=%s",
                    frame_event.frame_reference,
                    exc,
                )

        # -----------------------------------------------------
        # MINIO
        # -----------------------------------------------------

        return storage.download(
            "snapshots",
            frame_event.frame_reference,
        )

    # =========================================================
    # CAMERA SHAKE SUBSCRIBER
    # =========================================================

    async def _shake_subscriber(
        self,
    ) -> None:
        """
        Subscribe to camera shake notifications.

        CameraProfile is NOT used.

        The camera_id itself identifies the tracker.
        """

        redis_client = (
            await aioredis.from_url(
                (
                    f"redis://"
                    f"{settings.redis_host}:"
                    f"{settings.redis_port}"
                )
            )
        )

        pubsub = redis_client.pubsub()

        channel = (
            f"camera:shake:"
            f"{settings.test_camera_id}"
        )

        await pubsub.subscribe(
            channel
        )

        logger.info(
            "shake_subscriber_ready "
            "channel=%s",
            channel,
        )

        try:

            async for message in (
                pubsub.listen()
            ):

                if (
                    message["type"]
                    != "message"
                ):
                    continue

                logger.warning(
                    "camera_shake_received "
                    "camera_id=%s",
                    settings.test_camera_id,
                )

                self._tracker_manager.on_shake(
                    settings.test_camera_id
                )

        except asyncio.CancelledError:

            raise

        finally:

            try:
                await pubsub.unsubscribe(
                    channel
                )
            except Exception:
                pass

            try:
                await redis_client.close()
            except Exception:
                pass