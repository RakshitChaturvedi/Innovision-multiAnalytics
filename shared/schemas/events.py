from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from shared.schemas.common import BoundingBox, TrackResult
from shared.schemas.enums import (
    CameraProfile,
    IdentityTag,
    AlertType,
    AlertSeverity
)

class FrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    camera_profile: CameraProfile
    frame_seq: int = Field(..., ge=0)
    frame_object_key: str
    frame_shape: Tuple[int, int]
    timestamp: datetime = Field(default_factory=_utcnow)
    camera_shake: bool = Field(default=False)


class DetectionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: str
    camera_profile: CameraProfile
    frame_object_key: str
    frame_seq: int
    timestamp: datetime = Field(default_factory=_utcnow)
    tracks: List[TrackResult] = Field(default_factory=list)


class RecognitionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: str
    camera_profile: CameraProfile
    track_id: int
    person_id: Optional[UUID] = Field(default=None)
    identity_tag: IdentityTag
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    embedding_id: Optional[UUID] = Field(default=None)
    quality_score: float = Field(..., ge=0.0, le=1.0)
    liveness_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    frame_object_key: str
    timestamp: datetime = Field(default_factory=_utcnow)


class ZoneEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    zone_id: UUID
    camera_id: str
    track_id: int
    person_id: Optional[UUID] = Field(default=None)
    event_type: ZoneEventType
    dwell_duration: Optional[float] = Field(default=None, ge=0.0)
    timestamp: datetime = Field(default_factory=_utcnow)


class AlertEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    alert_type: AlertType
    severity: AlertSeverity
    camera_id: str
    zone_id: Optional[UUID] = Field(default=None)
    person_id: Optional[UUID] = Field(default=None)
    track_id: Optional[int] = Field(default=None)
    similarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    snapshot_object_key: Optional[str] = Field(default=None)
    status: AlertStatus = Field(default=AlertStatus.PENDING)
    created_at: datetime = Field(default_factory=_utcnow)