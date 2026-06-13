from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from shared.schemas.common import BoundingBox, TrackResult
from shared.schemas.enums import (
    AlertSeverity,
    AlertType,
    CameraProfile,
    CrowdModel,
    EventType,
    IdentityTag,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    timestamp: datetime = Field(default_factory=_utcnow)
    frame_seq: int = Field(..., ge=0)
    frame_object_key: str
    frame_shape: Tuple[int, int]
    profile: CameraProfile


class DetectionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    frame_event_id: UUID
    timestamp: datetime = Field(default_factory=_utcnow)
    frame_object_key: str
    frame_shape: Tuple[int, int]
    tracks: List[TrackResult] = Field(default_factory=list)
    profile: CameraProfile
    inference_latency_ms: float


class RecognitionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    detection_event_id: UUID
    track_id: int
    timestamp: datetime = Field(default_factory=_utcnow)
    identity_tag: IdentityTag
    person_id: Optional[UUID] = Field(default=None)
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    embedding_id: Optional[UUID] = Field(default=None)
    quality_score: float = Field(..., ge=0.0, le=1.0)
    liveness_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    liveness_checked: bool = Field(default=False)


class ZoneEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    zone_id: UUID
    track_id: int
    person_id: Optional[UUID] = Field(default=None)
    global_id: Optional[UUID] = Field(default=None)
    timestamp: datetime = Field(default_factory=_utcnow)
    event_type: str
    dwell_duration_seconds: Optional[float] = Field(default=None, ge=0.0)


class CrowdFrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    frame_object_key: str
    frame_shape: Tuple[int, int]
    timestamp: datetime = Field(default_factory=_utcnow)
    crowd_model: CrowdModel
    zone_ids: List[UUID] = Field(default_factory=list)


class AlertEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    alert_type: AlertType
    severity: AlertSeverity
    camera_id: UUID
    zone_id: Optional[UUID] = Field(default=None)
    person_id: Optional[UUID] = Field(default=None)
    track_id: int
    global_id: Optional[UUID] = Field(default=None)
    similarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    snapshot_object_key: Optional[str] = Field(default=None)
    timestamp: datetime = Field(default_factory=_utcnow)
    confidence: float = Field(..., ge=0.0, le=1.0)
    requires_human_verification: bool = Field(default=False)
    metadata: Dict = Field(default_factory=dict)