from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from shared.schemas.common import TrackResult
from shared.schemas.enums import (
    AlertSeverity,
    AlertType,
    CameraProfile,
    IdentityTag,
    CrowdModel
)

class FrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    timestamp: datetime
    frame_seq: int = Field(ge=0)
    frame_object_key: str
    frame_shape: tuple[int, int]
    profile: CameraProfile


class DetectionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    frame_event_id: UUID
    timestamp: datetime
    frame_object_key: str
    frame_shape: tuple[int, int]
    tracks: List[TrackResult]
    profile: CameraProfile
    inference_latency_ms: float


class RecognitionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    detection_event_id: UUID
    track_id: int
    timestamp: datetime
    identity_tag: IdentityTag
    person_id: Optional[UUID] = None
    similarity_score: float = Field(ge=0.0, le=1.0)
    embedding_id: Optional[UUID] = None
    quality_score: float = Field(ge=0.0, le=1.0)
    liveness_score: Optional[float] = None
    liveness_checked: bool = False


class ZoneEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    zone_id: UUID
    track_id: int
    person_id: Optional[UUID] = None
    global_id: Optional[UUID] = None
    timestamp: datetime
    event_type: str
    dwell_duration_seconds: Optional[float] = None


class CrowdFrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    frame_object_key: str
    frame_shape: tuple[int, int]
    timestamp: datetime 
    crowd_model: CrowdModel
    zone_ids: List[UUID] = Field(default_factory=list)


class AlertEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    alert_type: AlertType
    severity: AlertSeverity
    camera_id: UUID
    zone_id: Optional[UUID] = None
    person_id: Optional[UUID] = None
    track_id: int
    global_id: Optional[UUID] = None
    similarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    snapshot_object_key: Optional[str] = None
    timestamp: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_verification: bool = False
    metadata: dict = Field(default_factory=dict)