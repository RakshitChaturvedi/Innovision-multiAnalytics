from typing import Optional
from pydantic import BaseModel, Field

class BoundingBox(BaseModel):
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.x1 + self.x2)/2, (self.y1 + self.y2)/2)

    @property
    def area(self) -> float:
        return (self.x2 - self.x1)*(self.y2-self.y1)


class TrackResult(BaseModel):
    track_id: int
    bbox: BoundingBox 
    confidence: float = Field(ge=0.0, le=1.0)
    class_label: str 
    has_face: bool = False
    face_bbox: Optional[BoundingBox] = None