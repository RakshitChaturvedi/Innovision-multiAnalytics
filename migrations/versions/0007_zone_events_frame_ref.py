"""add frame_reference to zone_events

Revision ID: 0007_zone_events_frame_ref
Revises: 0006_reliability
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_zone_events_frame_ref"
down_revision = "0006_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "zone_events",
        sa.Column("frame_reference", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("zone_events", "frame_reference")
