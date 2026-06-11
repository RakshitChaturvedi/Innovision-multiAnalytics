from __future__ import annotations

from typing import Tuple

from pydantic import BaseModel, Field, model_validator


class BoundingBox(BaseModel):
    x1: float = Field(..., ge=0.0, le=1.0)
    y1: float = Field(..., ge=0.0, le=1.0)
    x2: float = Field(..., ge=0.0, le=1.0)
    y2: float = Field(..., ge=0.0, le=1.0)
    confidence: float = Field(..., ge=0.0, le=1.0)

    @model_validator(mode="after")
    def check_box_orientation(self) -> "BoundingBox":
        if self.x2 <= self.x1:
            raise ValueError(f"x2 ({self.x2}) must be greater than x1 ({self.x1})")
        if self.y2 <= self.y1:
            raise ValueError(f"y2 ({self.y2}) must be greater than y1 ({self.y1})")
        return self

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2


class TrackResult(BaseModel):
    track_id: int = Field(..., description="Persistent tracker-assigned ID")
    bbox: BoundingBox = Field(..., description="Normalized bounding box of detected person")
    class_label: str = Field(default="person")
    has_face: bool = Field(default=False)
    frame_object_key: str = Field(..., description="MinIO object key of the source frame")