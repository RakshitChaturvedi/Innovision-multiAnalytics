"""headcount breach state: status, resolution reason, publish flag, one open per zone

Revision ID: 0006_headcount_breach_state
Revises: 0005_headcount
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_headcount_breach_state"
down_revision = "0005_headcount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "headcount_breach_events",
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    )
    op.add_column(
        "headcount_breach_events",
        sa.Column("resolution_reason", sa.String(32), nullable=True),
    )
    op.add_column(
        "headcount_breach_events",
        sa.Column(
            "alert_published", sa.Boolean, nullable=False, server_default=sa.false()
        ),
    )

    op.execute(
        "UPDATE headcount_breach_events SET status = 'resolved' "
        "WHERE alert_status = 'resolved'"
    )
    # Rows written before this migration were assumed already alerted.
    op.execute(
        "UPDATE headcount_breach_events SET alert_published = true "
        "WHERE status = 'open'"
    )
    # Old code could leave several open rows per zone: keep the newest only.
    op.execute(
        """
        UPDATE headcount_breach_events b
        SET status = 'resolved', alert_status = 'resolved',
            resolved_at = COALESCE(b.resolved_at, now()),
            resolution_reason = 'superseded'
        WHERE b.status = 'open' AND EXISTS (
            SELECT 1 FROM headcount_breach_events n
            WHERE n.zone_id = b.zone_id AND n.status = 'open'
              AND (n.timestamp, n.id) > (b.timestamp, b.id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_headcount_breach_open_zone "
        "ON headcount_breach_events (zone_id) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_headcount_breach_open_zone")
    op.drop_column("headcount_breach_events", "alert_published")
    op.drop_column("headcount_breach_events", "resolution_reason")
    op.drop_column("headcount_breach_events", "status")
