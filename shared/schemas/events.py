from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from shared.schemas.common import TrackResult
from shared.schemas.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    CameraProfile,
    IdentityTag,
    CrowdModel,
    FrameProvider,
    EventType
)

class FrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    timestamp: datetime
    frame_seq: int = Field(ge=0)
    frame_reference: str
    frame_provider: FrameProvider
    frame_shape: tuple[int, int]
    profile: CameraProfile


class DetectionEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    frame_event_id: UUID
    timestamp: datetime
    frame_reference: str
    frame_seq: int
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
    frame_reference: str
    frame_seq: int
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
    frame_seq: int
    track_id: int
    person_id: Optional[UUID] = None
    global_id: Optional[UUID] = None
    timestamp: datetime
    event_type: EventType
    dwell_duration_seconds: Optional[float] = None


class CrowdFrameEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    frame_reference: str
    frame_shape: tuple[int, int]
    frame_seq: int
    timestamp: datetime 
    crowd_model: CrowdModel
    zone_ids: List[UUID] = Field(default_factory=list)


class AlertEvent(BaseModel):
    alert_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    timestamp: datetime
    severity: AlertSeverity
    alert_type: AlertType
    title: str = "Alert"
    description: Optional[str] = None
    source_event_ids: list[UUID]
    frame_reference: Optional[str] = None
    status: AlertStatus = AlertStatus.PENDING
    metadata: dict = Field(default_factory=dict)