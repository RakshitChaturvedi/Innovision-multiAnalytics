"""recognition: add refined_face_bbox to face_embeddings

Revision ID: 0004
Revises: 0003

WHY THIS MIGRATION EXISTS
--------------------------
detection_events.face_bbox (from 0002) is only ever an ESTIMATE —
face_estimator.py computes it as a fixed fraction of the person's YOLO
box; it never actually looks at the image. The real face location is
only known once SCRFD runs, inside the recognition service's
model_loader.detect_best_face() — but until this migration, that refined
box was computed and then discarded; nothing stored it.

This column holds that refined box, mapped from the crop's local pixel
space back onto the full frame and normalized to 0-1, matching the same
coordinate convention used everywhere else in the pipeline
(track.face_bbox, detection_events.face_bbox).

Lives on face_embeddings, not recognition_events — same reasoning as why
the embedding itself lives here: this is the row created once per
extraction (both real-time recognition AND future enrollment uploads),
so the box that specific embedding actually came from belongs next to it.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "face_embeddings",
        sa.Column("refined_face_bbox", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("face_embeddings", "refined_face_bbox")