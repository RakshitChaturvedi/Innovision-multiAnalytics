"""intruder alert cooldown: intruder_events.alert_suppressed

An intruder event recorded inside INTRUDER_ALERT_COOLDOWN_S of an earlier
published alert for the same camera+zone is kept but not published;
alert_suppressed = true says so (and stops a retry from publishing it).

Revision ID: 0008_intruder_alert_cooldown
Revises: 0007_headcount_breach_state
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_intruder_alert_cooldown"
down_revision = "0007_headcount_breach_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "intruder_events",
        sa.Column(
            "alert_suppressed", sa.Boolean, nullable=False, server_default=sa.false()
        ),
    )
    # cooldown lookup: last published alert for a camera+zone by event time
    op.create_index(
        "ix_intruder_events_published_cam_zone",
        "intruder_events",
        ["camera_id", "zone_id", "first_detected_at"],
        postgresql_where=sa.text("alert_published"),
    )


def downgrade() -> None:
    op.drop_index("ix_intruder_events_published_cam_zone", table_name="intruder_events")
    op.drop_column("intruder_events", "alert_suppressed")
