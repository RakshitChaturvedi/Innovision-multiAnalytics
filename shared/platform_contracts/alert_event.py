from __future__ import annotations

from pydantic import BaseModel, Field, model_validator, ConfigDict
from uuid import UUID, uuid4
from datetime import datetime
from typing import Optional

from shared.platform_contracts.enums import AlertSeverity, AlertStatus, FrameProvider, SourceUC

class AlertEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    alert_id: UUID = Field(default_factory=uuid4)
    camera_id: UUID
    timestamp: datetime
    severity: AlertSeverity
    alert_type: str

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    source_event_id: UUID
    source_uc: SourceUC
    frame_reference: Optional[str] = None
    frame_provider: Optional[FrameProvider] = None
    status: AlertStatus = AlertStatus.PENDING
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_frame_reference_consistency(self) -> AlertEvent:
        # frame_reference and frame_provider must either bost exist or both omitted.

        has_ref = self.frame_reference is not None
        has_provider = self.frame_provider is not None

        if has_ref != has_provider:
            raise ValueError(
                "frame_reference and frame_provider must both be set or both omitted"
                f"Got frame_reference={self.frame_reference!r},"
                f"frame_provider={self.frame_provider!r}"
            )
        return self

class AlertEventValidator:
    @staticmethod
    def validate(event: AlertEvent, known_cam_ids: set[UUID]) -> list[str]:
        errors = []

        if event.camera_id not in known_cam_ids:
            errors.append(f"camera_id {event.camera_id} not registered in Camera Registry")
        if not event.title.strip():
            errors.append("title cant be whitespace only")
        if not event.description.strip():
            errors.append("description cant be whitespace only")
        if event.title.startswith("[STUB]") and False:
            errors.append("stub alerts not allowed in production")
        return errors