"""detection_events table

Revision ID: 0002
Revises: 0001

WHY THIS MIGRATION EXISTS
-------------------------
The detection consumer INSERTs into `detection_events`, but no
migration ever created that table. Every batch that produced a track
failed with UndefinedTableError; the exception was caught and logged,
so the service looked healthy while persisting nothing.

Design notes
------------
- (camera_id, frame_seq, track_id) is UNIQUE so that the consumer's
  ON CONFLICT DO NOTHING is meaningful: after a redelivery the same
  detection is not written twice.
- bounding_box / face_bbox are JSONB because the publisher emits the
  BoundingBox pydantic model verbatim.
- frame_timestamp is TIMESTAMPTZ; the index on it supports the
  "last N minutes of activity" queries the dashboard will need.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        "detection_events",

        sa.Column(
            "id",
            UUID,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),

        sa.Column("camera_id", UUID, nullable=False),

        sa.Column("track_id", sa.Integer, nullable=False),

        sa.Column("bounding_box", JSONB, nullable=False),

        sa.Column("class_label", sa.String(64), nullable=False),

        sa.Column("confidence", sa.Float, nullable=False),

        sa.Column(
            "has_face",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),

        sa.Column("face_bbox", JSONB, nullable=True),

        sa.Column("frame_reference", sa.Text, nullable=False),

        sa.Column("frame_provider", sa.String(32), nullable=True),

        sa.Column("frame_seq", sa.BigInteger, nullable=False),

        sa.Column(
            "frame_timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),

        sa.Column("inference_latency_ms", sa.Float, nullable=True),

        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),

        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_detection_events_confidence_range",
        ),
    )

    # Idempotency guard for redelivered stream messages.
    op.create_unique_constraint(
        "uq_detection_events_camera_frame_track",
        "detection_events",
        ["camera_id", "frame_seq", "track_id"],
    )

    # Timeline queries: "what happened on camera X recently".
    op.create_index(
        "ix_detection_events_camera_time",
        "detection_events",
        ["camera_id", "frame_timestamp"],
    )

    # Track-history lookups from the recognition / zone services.
    op.create_index(
        "ix_detection_events_camera_track",
        "detection_events",
        ["camera_id", "track_id"],
    )

    # Recognition worker only cares about rows with a usable face.
    op.create_index(
        "ix_detection_events_has_face",
        "detection_events",
        ["camera_id", "frame_timestamp"],
        postgresql_where=sa.text("has_face"),
    )


def downgrade() -> None:
    op.drop_index("ix_detection_events_has_face", table_name="detection_events")
    op.drop_index("ix_detection_events_camera_track", table_name="detection_events")
    op.drop_index("ix_detection_events_camera_time", table_name="detection_events")
    op.drop_constraint(
        "uq_detection_events_camera_frame_track",
        "detection_events",
        type_="unique",
    )
    op.drop_table("detection_events")
