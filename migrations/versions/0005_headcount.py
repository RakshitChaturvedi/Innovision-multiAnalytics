"""add headcount tables

Revision ID: 0005_headcount
Revises: 0004, 0004_intruder
Create Date: 2026-09-27 18:40:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0005_headcount"
down_revision = ("0004_intruder", "0004")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------
    # Headcount Snapshots
    # ---------------------------------------------------------------
    op.create_table(
        "headcount_snapshots",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("camera_id", UUID, nullable=False),
        sa.Column("zone_id", UUID, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("rolling_avg", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_headcount_snapshots_camera_time",
        "headcount_snapshots",
        ["camera_id", "timestamp"],
    )

    op.create_index(
        "ix_headcount_snapshots_zone_time",
        "headcount_snapshots",
        ["zone_id", "timestamp"],
    )

    # ---------------------------------------------------------------
    # Headcount Breach Events
    # ---------------------------------------------------------------
    op.create_table(
        "headcount_breach_events",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("zone_id", UUID, nullable=False),
        sa.Column("camera_id", UUID, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("threshold", sa.Integer, nullable=False),
        sa.Column(
            "alert_status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_headcount_breach_zone_status",
        "headcount_breach_events",
        ["zone_id", "alert_status"],
    )

def downgrade() -> None:
    op.drop_index("ix_headcount_breach_zone_status", table_name="headcount_breach_events")
    op.drop_table("headcount_breach_events")

    op.drop_index("ix_headcount_snapshots_zone_time", table_name="headcount_snapshots")
    op.drop_index("ix_headcount_snapshots_camera_time", table_name="headcount_snapshots")
    op.drop_table("headcount_snapshots")
