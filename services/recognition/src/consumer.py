"""
Recognition consumer.

Consumes DetectionEvents. For each face track that passes sampling and
the quality gate:
  → runs the model exactly ONCE per crop (detection + embedding together)
  → maps SCRFD's crop-local face box back onto the full frame and stores it
  → searches the in-memory enrolled-embedding cache (no DB hit)
  → writes embedding + recognition event to DB, atomically, with audit
  → publishes RecognitionEvent to events:recognitions after commit

CameraProfile-based branching has been removed entirely — the detection
worker no longer emits a profile field on DetectionEvent, and this
service runs a single model pack against every camera in scope. See
model_loader.py for the single-pack loading logic.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import cv2
import numpy as np
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.audit.writer import AuditWriter
from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.enums import IdentityTag
from shared.schemas.events import DetectionEvent, RecognitionEvent
from shared.storage.storage_minio_client import StorageClient

from .camera_config_store import CameraConfigStore
from .config import config
from .embedding_cache import EmbeddingCache
from .embedding_extractor import extract_from_face
from .face_crop import FaceCropper
from .model_loader import RecognitionModelLoader
from .quality_gate import QualityGate
from .sampling import RecognitionSampler

logger = logging.getLogger(__name__)

# shared/storage/storage_minio_client.py exports the StorageClient class
# only, no module-level singleton, so it's instantiated once here.
# NOTE: StorageClient.buckets is a *list* indexed by position, not a dict
# keyed by name — 0 is "innovision-snapshots" as currently defined in that
# file. If that ever gets reordered or converted to a dict, this constant
# needs to move with it.
_storage = StorageClient()
SNAPSHOTS_BUCKET = 0


class RecognitionConsumer(BaseStreamConsumer):
    def __init__(self) -> None:
        super().__init__(
            stream_key=config.DETECTIONS_STREAM,
            group_name=config.CONSUMER_GROUP,
            consumer_name=config.CONSUMER_NAME,
        )
        self._model_loader = RecognitionModelLoader()
        self._cache = EmbeddingCache()
        self._cropper = FaceCropper()
        self._quality_gate = QualityGate()
        self._sampler = RecognitionSampler(
            sample_rate=config.DEFAULT_SAMPLE_RATE,
            quality_improvement_threshold=config.QUALITY_IMPROVEMENT_THRESHOLD,
            stale_ttl_seconds=config.STALE_TRACK_TTL_SECONDS,
        )
        self._cam_config = CameraConfigStore()
        self._engine = create_async_engine(config.DATABASE_URL)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._publisher: aioredis.Redis | None = None
        self._sweeper_task: asyncio.Task | None = None

    async def start(self) -> None:
        # Blocking model load — run once, before accepting any messages.
        self._model_loader.preload()

        redis_client = await aioredis.from_url(f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}")
        await self._cache.initialize(redis_client)
        await self._cam_config.initialize(redis_client)
        self._publisher = redis_client

        self._sweeper_task = asyncio.create_task(self._stale_track_sweeper())

        logger.info(
            "recognition_consumer_ready pack=%s enrolled=%d",
            config.RECOGNITION_MODEL_PACK, len(self._cache.enrolled_persons),
        )
        await super().start()

    async def stop(self) -> None:
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
        await super().stop()
        await self._engine.dispose()

    async def process(self, msg_id: str, data: dict) -> None:
        raw = data.get(b"data") or data.get("data")
        if raw is None:
            logger.error("detection_event_missing_data msg_id=%s", msg_id)
            return
        if isinstance(raw, bytes):
            raw = raw.decode()

        detection_event = DetectionEvent.model_validate_json(raw)

        face_tracks = [
            t for t in detection_event.tracks if t.has_face and t.face_bbox is not None
        ]
        if not face_tracks:
            return

        cam_cfg = await self._cam_config.get(str(detection_event.camera_id))

        try:
            frame_bytes = await self._fetch_frame(detection_event.frame_reference)
        except Exception as exc:
            logger.error(
                "frame_fetch_failed ref=%s error=%s",
                detection_event.frame_reference, exc,
            )
            raise  # do not ack — Redis will redeliver

        frame_array = np.frombuffer(frame_bytes, dtype=np.uint8)
        frame = cv2.imdecode(frame_array, cv2.IMREAD_COLOR)
        if frame is None:
            logger.error("frame_decode_failed ref=%s", detection_event.frame_reference)
            return  # ack — a corrupt frame won't improve on retry

        for track in face_tracks:
            try:
                await self._process_track(
                    detection_event=detection_event, track=track, frame=frame, cam_cfg=cam_cfg,
                )
            except Exception:
                # One bad track must not block the other tracks in this
                # frame, and must not force the whole DetectionEvent (and
                # every track in it) to be redelivered forever.
                logger.exception(
                    "track_processing_failed camera=%s track=%d",
                    detection_event.camera_id, track.track_id,
                )

    async def _process_track(self, detection_event: DetectionEvent, track, frame, cam_cfg: dict) -> None:
        camera_id = str(detection_event.camera_id)

        if not self._sampler.should_sample(
            camera_id=camera_id,
            track_id=track.track_id,
            frame_seq=detection_event.frame_seq,
            quality_score=track.confidence,
        ):
            return

        crop_result = self._cropper.crop(frame, track.face_bbox)
        if crop_result is None:
            return

        # Cheap, model-free checks first — no point spending an inference
        # call on a crop that's already too small or too blurry.
        precheck = self._quality_gate.precheck_size_blur(
            face_crop=crop_result.image,
            min_face_size_px=cam_cfg.get("min_face_size_px", config.DEFAULT_MIN_FACE_SIZE_PX),
            blur_threshold=cam_cfg.get("blur_threshold", config.DEFAULT_BLUR_THRESHOLD),
        )
        if not precheck.passes:
            logger.debug(
                "quality_precheck_rejected track=%d reason=%s",
                track.track_id, precheck.rejection_reason,
            )
            return

        # Single model call for this crop — detection, alignment, and the
        # 512-d embedding all come from this one Face object. Blocking
        # (CPU/GPU-bound), so it runs off the event loop.
        face = await asyncio.to_thread(self._model_loader.detect_best_face, crop_result.image)
        if face is None:
            logger.debug("no_face_detected_by_model track=%d", track.track_id)
            return

        quality_result = self._quality_gate.evaluate_face(
            face=face,
            blur_score=precheck.blur_score,
            face_size_px=precheck.face_size_px,
            pose_yaw_max=config.DEFAULT_POSE_YAW_MAX,
            pose_pitch_max=config.DEFAULT_POSE_PITCH_MAX,
            detector_confidence_min=config.DEFAULT_DETECTOR_CONFIDENCE_MIN,
        )
        if not quality_result.passes:
            logger.debug(
                "quality_gate_rejected track=%d reason=%s",
                track.track_id, quality_result.rejection_reason,
            )
            return

        embedding = extract_from_face(face)
        if embedding is None:
            return

        # face.bbox from InsightFace is in the CROP's own pixel space
        # (origin (0,0) = crop_result's top-left corner), not the frame's.
        # crop_result.x1/y1 is the exact offset FaceCropper used to slice
        # the crop out of the frame, so adding it back gives frame-pixel
        # coordinates, which are then normalized to 0-1 for storage —
        # same convention as every other bbox in this pipeline
        # (track.face_bbox, detection_events.face_bbox, etc).
        frame_h, frame_w = frame.shape[:2]
        fx1, fy1, fx2, fy2 = face.bbox
        refined_face_bbox = {
            "x1": max(0.0, min((crop_result.x1 + float(fx1)) / frame_w, 1.0)),
            "y1": max(0.0, min((crop_result.y1 + float(fy1)) / frame_h, 1.0)),
            "x2": max(0.0, min((crop_result.x1 + float(fx2)) / frame_w, 1.0)),
            "y2": max(0.0, min((crop_result.y1 + float(fy2)) / frame_h, 1.0)),
        }

        similarity_threshold = cam_cfg.get("similarity_threshold", config.VISITOR_THRESHOLD)
        matched_person, similarity_score = self._cache.search(
            query_embedding=embedding, threshold=similarity_threshold,
        )

        if matched_person is None:
            identity_tag, person_id = IdentityTag.UNKNOWN, None
        elif similarity_score >= config.ENROLLED_THRESHOLD:
            identity_tag, person_id = IdentityTag.ENROLLED, matched_person["person_id"]
        else:
            identity_tag, person_id = IdentityTag.VISITOR, matched_person["person_id"]

        logger.info(
            "recognition camera=%s track=%d tag=%s similarity=%.3f person=%s",
            camera_id, track.track_id, identity_tag.value, similarity_score,
            matched_person["name"] if matched_person else "unknown",
        )

        await self._persist_and_publish(
            detection_event=detection_event,
            track_id=track.track_id,
            embedding=embedding,
            quality_score=quality_result.quality_score,
            identity_tag=identity_tag,
            person_id=person_id,
            similarity_score=similarity_score,
            refined_face_bbox=refined_face_bbox,
        )

    async def _persist_and_publish(
        self,
        detection_event: DetectionEvent,
        track_id: int,
        embedding: np.ndarray,
        quality_score: float,
        identity_tag: IdentityTag,
        person_id: str | None,
        similarity_score: float,
        refined_face_bbox: dict,
    ) -> None:
        embedding_id = str(uuid.uuid4())
        recognition_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # pgvector's text input format is "[v1,v2,...]" — a bare Python
        # list bound as a param does not reliably cast to a `vector`
        # column over asyncpg, so it's serialized explicitly and cast
        # in the query.
        embedding_literal = "[" + ",".join(f"{v:.8f}" for v in embedding.tolist()) + "]"

        # Same reasoning as the vector cast above — asyncpg does not
        # reliably adapt a raw Python dict to `jsonb` via a text() query
        # param, so it's serialized explicitly and cast, matching the
        # pattern the detection worker already uses for its own JSONB
        # columns (bounding_box / face_bbox in detection_events).
        refined_face_bbox_json = json.dumps(refined_face_bbox)

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("""
                    INSERT INTO face_embeddings (
                        id, person_id, embedding, source_camera_id, quality_score,
                        is_enrollment, refined_face_bbox
                    ) VALUES (
                        :id, :person_id, CAST(:embedding AS vector), :source_camera_id, :quality_score,
                        false, CAST(:refined_face_bbox AS jsonb)
                    )
                """), {
                    "id": embedding_id,
                    "person_id": person_id,
                    "embedding": embedding_literal,
                    "source_camera_id": str(detection_event.camera_id),
                    "quality_score": quality_score,
                    "refined_face_bbox": refined_face_bbox_json,
                })

                await session.execute(text("""
                    INSERT INTO recognition_events (
                        id, camera_id, detection_event_id, track_id,
                        person_id, similarity_score, identity_tag,
                        embedding_id, quality_score, liveness_checked, timestamp
                    ) VALUES (
                        :id, :camera_id, :detection_event_id, :track_id,
                        :person_id, :similarity_score, :identity_tag,
                        :embedding_id, :quality_score, false, :timestamp
                    )
                """), {
                    "id": recognition_id,
                    "camera_id": str(detection_event.camera_id),
                    "detection_event_id": str(detection_event.event_id),
                    "track_id": track_id,
                    "person_id": person_id,
                    "similarity_score": similarity_score,
                    "identity_tag": identity_tag.value,
                    "embedding_id": embedding_id,
                    "quality_score": quality_score,
                    "timestamp": now,
                })

                if person_id:
                    await session.execute(text("""
                        UPDATE enrolled_persons SET last_seen_at = :now WHERE id = :person_id
                    """), {"now": now, "person_id": person_id})

                audit = AuditWriter(session)
                await audit.log(
                    service="recognition",
                    action="embedding_written",
                    entity_type="face_embedding",
                    entity_id=embedding_id,
                    metadata={
                        "person_id": person_id,
                        "identity_tag": identity_tag.value,
                        "similarity_score": similarity_score,
                        "camera_id": str(detection_event.camera_id),
                    },
                )
            # transaction committed here — embedding, event, last_seen_at,
            # and audit log all land atomically or not at all

        recognition_event = RecognitionEvent(
            event_id=uuid.UUID(recognition_id),
            camera_id=detection_event.camera_id,
            detection_event_id=detection_event.event_id,
            track_id=track_id,
            timestamp=now,
            frame_reference=detection_event.frame_reference,
            frame_seq=detection_event.frame_seq,
            identity_tag=identity_tag,
            person_id=uuid.UUID(person_id) if person_id else None,
            similarity_score=similarity_score,
            embedding_id=uuid.UUID(embedding_id),
            quality_score=quality_score,
            liveness_score=None,
            liveness_checked=False,
        )

        await self._publisher.xadd(
            config.RECOGNITIONS_STREAM,
            {"data": recognition_event.model_dump_json()},
            maxlen=config.RECOGNITIONS_MAXLEN,
            approximate=True,
        )

    async def _fetch_frame(self, frame_reference: str) -> bytes:
        """
        DetectionEvent carries no frame_provider field (only FrameEvent
        does, upstream of detection, and it's not forwarded) — so there's
        currently no way for this service to know whether a frame lives
        in the Redis cache or MinIO. Falling back to MinIO only, since
        that's the durable store. If frames are meant to be readable from
        the Redis frame cache within recognition's processing window,
        frame_provider needs to be added back onto DetectionEvent and
        forwarded by the detection worker's publisher — same shape of fix
        as the CameraProfile field, just not a deliberate removal this
        time.
        """
        return _storage.download(SNAPSHOTS_BUCKET, frame_reference)

    async def _stale_track_sweeper(self) -> None:
        """
        No 'track ended' signal exists from the detection worker, so
        per-track sampler state is aged out on a timer instead of being
        retained forever.
        """
        interval = config.STALE_TRACK_SWEEP_INTERVAL_SECONDS
        while True:
            try:
                await asyncio.sleep(interval)
                self._sampler.sweep_stale()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("stale_track_sweep_failed")