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
import math
import uuid

import cv2
import numpy as np
import redis.asyncio as aioredis
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.audit.writer import AuditWriter
from shared.config import settings
from shared.errors import PermanentError
from shared.frames import FrameUnavailable, fetch_frame
from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.enums import IdentityTag
from shared.schemas.events import DetectionEvent, RecognitionEvent
from shared.storage.storage_minio_client import StorageClient

from .camera_config_store import CameraConfigStore
from .config import config
from .embedding_cache import EmbeddingCache
from .embedding_extractor import extract_from_face
from .face_crop import FaceCropper
from .face_selection import HeadRegionConfig, select_face
from .model_loader import RecognitionModelLoader
from .quality_gate import QualityGate
from .sampling import RecognitionSampler
from .supervised import supervise

logger = logging.getLogger(__name__)

# Deterministic ids: a redelivered DetectionEvent must land on the same rows
# (ON CONFLICT DO NOTHING) and re-publish the same RecognitionEvent id.
_ID_NAMESPACE = uuid.UUID("6f1c1f0e-5d1b-4c57-9a55-3f7f7f2d6a10")


def _clamp01(value: float) -> float:
    """Cosine similarity / quality can fall outside [0, 1]; the DB CHECK and
    the RecognitionEvent model only accept [0, 1]."""
    value = float(value)
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _decode_jpeg(frame_bytes: bytes):
    return cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)


class RecognitionConsumer(BaseStreamConsumer):
    def __init__(self) -> None:
        super().__init__(
            streams=[config.DETECTIONS_STREAM],
            group_name=config.CONSUMER_GROUP,
            consumer_name=config.CONSUMER_NAME,
        )
        self._model_loader = RecognitionModelLoader()
        self._cache = EmbeddingCache()
        self._cropper = FaceCropper()
        self._head_cfg = HeadRegionConfig(
            width_frac=config.HEAD_WIDTH_FRAC,
            top_margin_frac=config.HEAD_TOP_MARGIN_FRAC,
            height_frac=config.HEAD_HEIGHT_FRAC,
            ambiguity_ratio=config.FACE_AMBIGUITY_RATIO,
        )
        self._quality_gate = QualityGate()
        self._sampler = RecognitionSampler(
            sample_rate=config.DEFAULT_SAMPLE_RATE,
            quality_improvement_threshold=config.QUALITY_IMPROVEMENT_THRESHOLD,
            stale_ttl_seconds=config.STALE_TRACK_TTL_SECONDS,
        )
        self._cam_config = CameraConfigStore()
        self._engine = create_async_engine(config.DATABASE_URL)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._pubsub_redis: aioredis.Redis | None = None
        self._minio: StorageClient | None = None
        self._sweeper_task: asyncio.Task | None = None
        self._counters.update({"frame_unavailable": 0, "bad_payload": 0, "frame_decode_failed": 0})

    def _count(self, name: str) -> None:
        self._counters[name] = self._counters.get(name, 0) + 1

    async def start(self) -> None:
        # Blocking model load — run once, before accepting any messages.
        self._model_loader.preload()

        # Cold-copy frame source. Credentials are read lazily by StorageClient;
        # without MINIO_* set, fetch_frame simply has no cold fallback.
        self._minio = StorageClient()

        # Dedicated connection for the pub/sub listeners; the consumer's own
        # self.redis (created in super().start()) serves frames and XADD.
        self._pubsub_redis = aioredis.from_url(settings.redis_url(), health_check_interval=30)
        await self._cache.initialize(self._pubsub_redis)
        await self._cam_config.initialize(self._pubsub_redis)

        self._sweeper_task = asyncio.create_task(
            supervise("stale_track_sweeper", self._stale_track_sweeper),
            name="recognition-stale-sweeper",
        )

        logger.info(
            "recognition_consumer_ready pack=%s enrolled=%d",
            config.RECOGNITION_MODEL_PACK, len(self._cache.enrolled_persons),
        )
        await super().start()

    async def stop(self) -> None:
        """Safe to call at any point of start(), and more than once."""
        task, self._sweeper_task = self._sweeper_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        try:
            await super().stop()  # drains in-flight work while engines are still usable
        finally:
            for name, closer in (
                ("embedding_cache", self._cache.close),
                ("camera_config", self._cam_config.close),
                ("consumer_engine", self._engine.dispose),
                ("pubsub_redis", self._close_pubsub_redis),
            ):
                try:
                    await closer()
                except Exception:
                    logger.exception("recognition_stop_cleanup_failed component=%s", name)

    async def _close_pubsub_redis(self) -> None:
        client, self._pubsub_redis = self._pubsub_redis, None
        if client is not None:
            await client.aclose()

    def _parse_event(self, msg_id: str, data: dict) -> DetectionEvent:
        raw = data.get(b"data") or data.get("data")
        if raw is None:
            self._count("bad_payload")
            logger.warning("detection_event_missing_data msg_id=%s", msg_id)
            raise PermanentError(f"message {msg_id} has no 'data' field")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return DetectionEvent.model_validate_json(raw)
        except ValidationError as exc:
            self._count("bad_payload")
            logger.warning("detection_event_invalid msg_id=%s error=%s", msg_id, exc)
            raise PermanentError(f"invalid DetectionEvent in {msg_id}: {exc}") from exc

    async def process(self, msg_id: str, data: dict, stream: str) -> None:
        detection_event = self._parse_event(msg_id, data)

        face_tracks = [
            t for t in detection_event.tracks if t.has_face and t.face_bbox is not None
        ]
        if not face_tracks:
            return

        cam_cfg = await self._cam_config.get(str(detection_event.camera_id))

        try:
            frame_bytes = await fetch_frame(
                self.redis,
                self._minio,
                camera_id=detection_event.camera_id,
                frame_seq=detection_event.frame_seq,
                frame_reference=detection_event.frame_reference,
                frame_provider=detection_event.frame_provider,
            )
        except FrameUnavailable:
            # Permanent: the base consumer dead-letters and acks. No retry loop.
            self._count("frame_unavailable")
            logger.warning(
                "frame_unavailable camera=%s seq=%s ref=%s",
                detection_event.camera_id, detection_event.frame_seq,
                detection_event.frame_reference,
            )
            raise

        frame = await asyncio.to_thread(_decode_jpeg, frame_bytes)
        if frame is None:
            self._count("frame_decode_failed")
            logger.warning("frame_decode_failed ref=%s", detection_event.frame_reference)
            raise PermanentError(f"frame not decodable: {detection_event.frame_reference}")

        failures: list[Exception] = []
        for track in face_tracks:
            try:
                await self._process_track(
                    detection_event=detection_event, track=track, frame=frame, cam_cfg=cam_cfg,
                )
            except PermanentError:
                raise
            except Exception as exc:
                # One bad track must not block the other tracks of this frame,
                # but the failure must not be swallowed either: forget the
                # track's sampler state (so the retry is not skipped by
                # sampling) and re-raise after the loop so the message stays
                # pending and is redelivered. Persistence is idempotent.
                logger.exception(
                    "track_processing_failed camera=%s track=%d",
                    detection_event.camera_id, track.track_id,
                )
                self._sampler.evict(str(detection_event.camera_id), track.track_id)
                failures.append(exc)
        if failures:
            raise failures[0]

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
        faces = await asyncio.to_thread(self._model_loader.detect_faces, crop_result.image)
        frame_h, frame_w = frame.shape[:2]
        # Crop-local face boxes -> frame-pixel centers, then assign faces to
        # tracks: the crop may hold a neighbour's face (identity swap).
        centers = [
            (
                crop_result.x1 + (float(f.bbox[0]) + float(f.bbox[2])) / 2,
                crop_result.y1 + (float(f.bbox[1]) + float(f.bbox[3])) / 2,
            )
            for f in faces
        ]
        selection = select_face(
            centers, track, detection_event.tracks, frame_w, frame_h, self._head_cfg,
        )
        if selection.index is None:
            self._count(f"face_{selection.reason}")
            logger.debug(
                "face_not_selected camera=%s track=%d faces=%d reason=%s",
                camera_id, track.track_id, len(faces), selection.reason,
            )
            return
        face = faces[selection.index]

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
        similarity_score = _clamp01(similarity_score)

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
            quality_score=_clamp01(quality_result.quality_score),
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
        # Deterministic ids: a redelivery hits ON CONFLICT and publishes the
        # very same RecognitionEvent again.
        embedding_id = str(uuid.uuid5(_ID_NAMESPACE, f"embedding:{detection_event.event_id}:{track_id}"))
        recognition_id = str(uuid.uuid5(_ID_NAMESPACE, f"recognition:{detection_event.event_id}:{track_id}"))
        # Business time is the event time, never the wall clock.
        now = detection_event.timestamp
        similarity_score = _clamp01(similarity_score)
        quality_score = _clamp01(quality_score)

        # pgvector's text input format is "[v1,v2,...]"; serialized explicitly
        # and cast in the query (asyncpg has no codec for it here).
        embedding_literal = "[" + ",".join(f"{v:.8f}" for v in embedding.tolist()) + "]"
        # Same for jsonb: explicit json.dumps + CAST.
        refined_face_bbox_json = json.dumps(refined_face_bbox)

        async with self._session_factory() as session:
            async with session.begin():
                inserted = await session.execute(text("""
                    INSERT INTO face_embeddings (
                        id, person_id, embedding, source_camera_id, quality_score,
                        is_enrollment, refined_face_bbox
                    ) VALUES (
                        :id, :person_id, CAST(:embedding AS vector), :source_camera_id, :quality_score,
                        false, CAST(:refined_face_bbox AS jsonb)
                    )
                    ON CONFLICT (id) DO NOTHING
                """), {
                    "id": embedding_id,
                    "person_id": person_id,
                    "embedding": embedding_literal,
                    "source_camera_id": str(detection_event.camera_id),
                    "quality_score": quality_score,
                    "refined_face_bbox": refined_face_bbox_json,
                })

                if inserted.rowcount == 0:
                    # Redelivery: the whole transaction already committed once.
                    logger.info(
                        "recognition_already_persisted event=%s track=%d",
                        detection_event.event_id, track_id,
                    )
                else:
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
                        ON CONFLICT (id) DO NOTHING
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

                    await AuditWriter(session).log(
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
            # Committed here: embedding, event, last_seen_at and audit row land
            # together or not at all.

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

        await self.redis.xadd(
            config.RECOGNITIONS_STREAM,
            {"data": recognition_event.model_dump_json()},
            maxlen=config.RECOGNITIONS_MAXLEN,
            approximate=True,
        )

    async def _stale_track_sweeper(self) -> None:
        """
        No 'track ended' signal exists from the detection worker, so
        per-track sampler state is aged out on a timer instead of being
        retained forever. Runs under supervise(): a failing sweep is logged
        and the loop restarted, never silent.
        """
        interval = config.STALE_TRACK_SWEEP_INTERVAL_SECONDS
        while True:
            await asyncio.sleep(interval)
            self._sampler.sweep_stale()
